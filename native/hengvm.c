/*
 * 衡语虚拟机（原生版）—— hengvm
 *
 * 这是衡语的执行内核，用 C 写成，不依赖 Python 或任何运行时。
 * 输入：.hbc 字节码文件（由 heng2 编译器生成）
 * 输出：程序的标准输出
 *
 * 值表示（统一 16 字节槽，避免动态类型标签的间接寻址）：
 *   tag: u8（NUM/TEXT/BOOL/NIL/SEQ/MAP/REC），
 *   num: double（NUM 用），
 *   ptr: void*（堆对象用）
 *
 * 设计取舍：
 *   - 数值统一 double，量纲在编译期已消除，运行期只算数；
 *   - 文本/序列/映射用引用计数堆对象；
 *   - 局部变量是连续数组，按编译期算出的槽位下标访问；
 *   - 调用栈是连续数组 + 帧指针，不使用递归（避免爆栈，且更快）。
 */

/* 特性宏必须在任何头文件之前定义。
   Linux 的 glibc 默认只暴露 ISO C 的一部分，
   readlink / getline 等 POSIX 函数需要 _POSIX_C_SOURCE 或 _DEFAULT_SOURCE，
   否则报 implicit declaration（而 macOS 默认全开，本地编译根本发现不了）。 */
#ifdef __linux__
#  ifndef _DEFAULT_SOURCE
#    define _DEFAULT_SOURCE 1
#  endif
#  ifndef _POSIX_C_SOURCE
#    define _POSIX_C_SOURCE 200809L
#  endif
#endif

#include <stdio.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <pthread.h>
#include <time.h>
#include "opcode_width.h"

static int g_trace = 0;

/* ============ 值 ============ */

typedef enum {
    T_NIL = 0, T_NUM, T_BOOL, T_TEXT, T_SEQ, T_MAP, T_REC, T_FUNC
} Tag;

typedef struct Obj Obj;

typedef struct {
    uint8_t tag;
    uint8_t pad[7];
    union {
        double num;
        int    b;
        Obj   *obj;
    } u;
} Val;

struct Obj {
    uint8_t kind;      /* Tag */
    uint8_t interned;  /* 1 = 驻留文本，永不释放 */
    uint32_t rc;       /* 引用计数 */
    /* 文本 */
    char  *text;
    uint32_t len;
    /* 序列 */
    Val   *items;
    uint32_t n;
    /* 映射：数组存键值 + 哈希索引加速查找。
       早期是纯线性扫描，注释写着「AI 场景的键通常很少」——
       100 项目验证推翻了这个假设：8 千次写入要 134ms（O(n^2)）。 */
    char **keys;
    Val   *vals;
    uint32_t cap;
    int32_t  *buckets;     /* 桶 → 条目下标（-1 空） */
    int32_t  *next;        /* 条目 → 同桶下一条目 */
    uint32_t nbuckets;     /* 桶数，2 的幂 */
    uint32_t used;         /* 已用条目（= n） */
    /* 记录 */
    const char *rec_name;
    uint32_t rec_index;
};

/* ============ 内存 ============ */

static Obj *obj_new(uint8_t kind) {
    Obj *o = (Obj *)calloc(1, sizeof(Obj));
    if (!o) { fprintf(stderr, "衡语：内存不足\n"); exit(70); }
    o->kind = kind;
    o->rc = 1;
    return o;
}

static void obj_release(Obj *o);

static void val_release(Val v) {
    if ((v.tag == T_TEXT || v.tag == T_SEQ || v.tag == T_MAP || v.tag == T_REC
         || v.tag == T_FUNC) && v.u.obj)
        obj_release(v.u.obj);
}

static void obj_release(Obj *o) {
    if (!o) return;
    if (o->interned) return;      /* 驻留对象常驻，由驻留表统一持有 */
    if (--o->rc > 0) return;
    if (o->kind == T_TEXT) {
        free(o->text);
    } else if (o->kind == T_SEQ) {
        for (uint32_t i = 0; i < o->n; i++) val_release(o->items[i]);
        free(o->items);
    } else if (o->kind == T_FUNC) {
        for (uint32_t i = 0; i < o->n; i++) val_release(o->items[i]);
        free(o->items);
    } else if (o->kind == T_MAP || o->kind == T_REC) {
        for (uint32_t i = 0; i < o->n; i++) {
            free(o->keys[i]);
            val_release(o->vals[i]);
        }
        free(o->keys);
        free(o->vals);
        free(o->buckets);
        free(o->next);
    }
    free(o);
}

static void val_retain(Val v) {
    if ((v.tag == T_TEXT || v.tag == T_SEQ || v.tag == T_MAP || v.tag == T_REC
         || v.tag == T_FUNC) && v.u.obj)
        v.u.obj->rc++;
}

/* ============ 构造 ============ */

static Val v_num(double d)   { Val v; memset(&v,0,sizeof v); v.tag=T_NUM;  v.u.num=d; return v; }
static Val v_bool(int b)     { Val v; memset(&v,0,sizeof v); v.tag=T_BOOL; v.u.b=b;   return v; }
static Val v_nil(void)       { Val v; memset(&v,0,sizeof v); v.tag=T_NIL; return v; }
static Val v_obj(Obj *o)     { Val v; memset(&v,0,sizeof v); v.tag=o->kind; v.u.obj=o; return v; }

/* ---- 字符串驻留 ----
   短文本（<= 24 字节）在进程内共享同一对象：
     - 相同内容只分配一次，减少 malloc 压力
     - 比较可以先比指针（后续可加快速路径）
   驻留对象 rc 不参与回收，由进程生命周期持有。 */
#define INTERN_MAX 24
#define INTERN_BUCKETS 4096

static Obj *g_intern[INTERN_BUCKETS];
static uint32_t g_intern_n;

static uint32_t str_hash_n(const char *s, uint32_t n) {
    uint32_t h = 2166136261u;
    for (uint32_t i = 0; i < n; i++) { h ^= (uint8_t)s[i]; h *= 16777619u; }
    return h;
}

static Val v_text_n(const char *s, uint32_t n) {
    if (n <= INTERN_MAX) {
        uint32_t b = str_hash_n(s, n) & (INTERN_BUCKETS - 1);
        for (Obj *p = g_intern[b]; p; p = (Obj *)p->buckets) {
            if (p->len == n && memcmp(p->text, s, n) == 0) {
                return v_obj(p);     /* 命中：复用，不增加 rc */
            }
        }
        Obj *o = obj_new(T_TEXT);
        o->text = (char *)malloc(n + 1);
        memcpy(o->text, s, n);
        o->text[n] = 0;
        o->len = n;
        o->interned = 1;
        o->rc = 0;
        o->buckets = (int32_t *)g_intern[b];   /* 复用字段做链 */
        g_intern[b] = o;
        g_intern_n++;
        return v_obj(o);
    }
    Obj *o = obj_new(T_TEXT);
    o->text = (char *)malloc(n + 1);
    memcpy(o->text, s, n);
    o->text[n] = 0;
    o->len = n;
    return v_obj(o);
}
static Val v_text(const char *s) { return v_text_n(s, (uint32_t)strlen(s)); }

/* ============ 错误 ============ */

/* 出错位置：主循环每次取指时更新，报错时一并输出，
   供上层把字节码偏移映射回源码行号（诊断质量的关键）。 */
static int  g_cur_pc = 0;
static int  g_cur_func = -1;

static void raise_err(const char *cat, const char *msg) {
    /* 位置前缀 @pc:func 便于上层映射源码行；无位置时省略 */
    if (g_cur_pc > 0 || g_cur_func >= 0)
        fprintf(stderr, "@%d:%d 错误<%s>: %s\n", g_cur_pc, g_cur_func,
                cat ? cat : "(null)", msg ? msg : "(null)");
    else
        fprintf(stderr, "错误<%s>: %s\n", cat ? cat : "(null)", msg ? msg : "(null)");
    exit(1);
}

/* 运行期错误：若当前有异常处理器，则记录并让主循环分派；
   否则直接退出。所有语言级错误（除零/越界/类型/未授权）都必须走这里，
   否则「有 try 却抓不住」——这正是先前异常全都漏出去的原因。 */
/* 传给衡语程序的命令行参数（argv[2] 之后是用户参数）。
   做工具时必须有——早期语言里根本读不到命令行参数。 */
static int   g_argc = 0;
static char **g_argv = NULL;

static char g_err_cat[64];
static char g_err_msg[1024];
static int  g_err_pending = 0;
static int  g_handler_n = 0;

static void runtime_error(const char *cat, const char *msg) {
    snprintf(g_err_cat, sizeof g_err_cat, "%s", cat ? cat : "未定义");
    snprintf(g_err_msg, sizeof g_err_msg, "%s", msg ? msg : "");
    if (g_handler_n == 0) raise_err(g_err_cat, g_err_msg);
    g_err_pending = 1;
}

/* ============ 字节码读取 ============ */

typedef struct {
    uint8_t kind;
    union { double num; char *text; int b; } v;
} Const;

typedef struct {
    char *name;
    uint16_t n_params;
    uint16_t n_slots;
    char *ret_kind;
    char *ret_unit;
    uint8_t *code;
    uint32_t code_len;
} Func;

typedef struct {
    char *name;
    uint16_t n_fields;
    char **fields;
} RecordDef;

static uint8_t *g_buf;
static size_t g_pos, g_size;

static void need(size_t n) {
    if (g_pos + n > g_size) raise_err("未定义", "字节码文件损坏或截断");
}
static uint8_t rd_u8(void)  { need(1); return g_buf[g_pos++]; }
static uint16_t rd_u16(void){ need(2); uint16_t v; memcpy(&v,g_buf+g_pos,2); g_pos+=2; return v; }
static uint32_t rd_u32(void){ need(4); uint32_t v; memcpy(&v,g_buf+g_pos,4); g_pos+=4; return v; }
static double rd_f64(void)  { need(8); double v; memcpy(&v,g_buf+g_pos,8); g_pos+=8; return v; }
static char *rd_str(void) {
    uint32_t n = rd_u32();
    need(n);
    char *s = (char *)malloc(n + 1);
    memcpy(s, g_buf + g_pos, n);
    s[n] = 0;
    g_pos += n;
    return s;
}

/* ============ 模块 ============ */

static Const  *g_pool;  static uint32_t g_pool_n;
static Func   *g_funcs; static uint32_t g_funcs_n;
static RecordDef *g_recs; static uint32_t g_recs_n;
static uint8_t *g_entry; static uint32_t g_entry_len;
static uint16_t g_n_globals, g_entry_slots;

/* FFI 库表：由 load_module 从字节码读入，ffi_call 使用时按需 dlopen */
typedef struct {
    char  alias[64];      /* 衡语侧别名，如「数学」 */
    char  lib[128];       /* 动态库名，如 libm / libz */
    void *handle;
} FfiLib;

static FfiLib *g_ffi_libs = NULL;
static int     g_ffi_n = 0;

/* ---- 内置 HTTP 服务端 ----
   用途：让衡语自己当 Web 后端。
   图形界面的思路是「衡语写全部逻辑 + 浏览器渲染」，
   没有服务端能力就只能是死板的命令行工具。
   单线程阻塞式：请求进来 → 回调处理 → 写响应。
   够用在本地开发工具与 IDE 上，不追求并发吞吐。 */
static int g_srv_fd = -1;

/* 异常处理：处理器栈 + 错误标志。
   THROW 不再 longjmp（跨帧 longjmp 会踩到已弹出的栈帧，实测 SIGBUS），
   而是置标志并在主循环顶部统一分派——语义等价，且完全可移植。 */
#define MAX_HANDLERS 64

typedef struct {
    int catch_pc;
    int finally_pc;
    int frame_depth;
    int slot_top;
    int stack_top;
} Handler;

static Handler g_handlers[MAX_HANDLERS];

/* 沙箱能力：受限标记 + 白名单。受限时 CAP_CHECK 必须命中白名单。 */
static int      g_sandbox_on = 0;
static char    *g_caps[64];
static int      g_caps_n = 0;
static int      g_cap_stack[64];
static int      g_cap_stack_n = 0;

/* 输出单位标签表：第 n 次 PRINT 对应的显示单位（空串表示不加） */
static char  **g_units;
static uint32_t g_units_n;
static uint32_t g_print_idx;

static void load_module(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "衡语：打不开字节码文件 %s\n", path); exit(1); }
    fseek(f, 0, SEEK_END);
    g_size = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    g_buf = (uint8_t *)malloc(g_size);
    if (fread(g_buf, 1, g_size, f) != g_size) { fprintf(stderr, "衡语：读取失败\n"); exit(1); }
    fclose(f);
    g_pos = 0;

    if (g_size < 6 || memcmp(g_buf, "HENG", 4) != 0)
        raise_err("未定义", "不是衡语字节码文件");
    if (g_buf[4] != 2)
        raise_err("未定义", "字节码版本不支持");
    g_pos = 6;

    g_pool_n = rd_u32();
    g_pool = (Const *)calloc(g_pool_n ? g_pool_n : 1, sizeof(Const));
    for (uint32_t i = 0; i < g_pool_n; i++) {
        uint8_t kind = rd_u8();
        g_pool[i].kind = kind;
        if (kind == 0)       g_pool[i].v.num = rd_f64();
        else if (kind == 2)  g_pool[i].v.b = rd_u8();
        else if (kind == 1 || kind == 3) g_pool[i].v.text = rd_str();
    }

    g_funcs_n = rd_u32();
    g_funcs = (Func *)calloc(g_funcs_n ? g_funcs_n : 1, sizeof(Func));
    for (uint32_t i = 0; i < g_funcs_n; i++) {
        uint32_t name_i = rd_u32();
        uint16_t np = rd_u16();
        uint16_t ns = rd_u16();
        /* 注意：ret_kind / ret_unit 在 Python 端是 u16 索引（H 格式） */
        uint16_t rk = rd_u16();
        uint16_t ru = rd_u16();
        uint32_t cl = rd_u32();
        need(cl);
        g_funcs[i].name = g_pool[name_i].v.text;
        g_funcs[i].n_params = np;
        g_funcs[i].n_slots = ns;
        g_funcs[i].ret_kind = g_pool[rk].v.text;
        g_funcs[i].ret_unit = g_pool[ru].v.text;
        g_funcs[i].code = g_buf + g_pos;
        g_funcs[i].code_len = cl;
        g_pos += cl;
    }

    g_recs_n = rd_u32();
    g_recs = (RecordDef *)calloc(g_recs_n ? g_recs_n : 1, sizeof(RecordDef));
    for (uint32_t i = 0; i < g_recs_n; i++) {
        uint32_t ni = rd_u32();
        uint16_t nf = rd_u16();
        g_recs[i].name = g_pool[ni].v.text;
        g_recs[i].n_fields = nf;
        g_recs[i].fields = (char **)calloc(nf ? nf : 1, sizeof(char *));
        for (uint16_t k = 0; k < nf; k++) {
            uint32_t fi = rd_u32();
            g_recs[i].fields[k] = g_pool[fi].v.text;
        }
    }

    g_entry_len = rd_u32();
    need(g_entry_len);
    g_entry = g_buf + g_pos;
    g_pos += g_entry_len;

    g_n_globals = rd_u16();
    g_entry_slots = rd_u16();

    g_units_n = rd_u32();
    g_units = (char **)calloc(g_units_n ? g_units_n : 1, sizeof(char *));
    for (uint32_t i = 0; i < g_units_n; i++) {
        g_units[i] = rd_str();   /* 直接内嵌字符串，不走常量池索引 */
    }

    /* FFI 库表（可选段）：老字节码没有这段，读到文件结尾即视为无 FFI */
    if (g_pos < g_size) {
        uint32_t nlib = rd_u32();
        g_ffi_n = (int)nlib;
        g_ffi_libs = (FfiLib *)calloc(nlib ? nlib : 1, sizeof(FfiLib));
        for (uint32_t i = 0; i < nlib; i++) {
            snprintf(g_ffi_libs[i].alias, sizeof g_ffi_libs[i].alias, "%s", rd_str());
            snprintf(g_ffi_libs[i].lib, sizeof g_ffi_libs[i].lib, "%s", rd_str());
            g_ffi_libs[i].handle = NULL;
        }
    }
}

/* ============ 指令码（与 heng2/bytecode.py 保持一致） ============ */

enum {
    OP_NOP=0x00, OP_HALT=0x01, OP_SELECT=0x1B,
    OP_PUSH_NUM=0x10, OP_PUSH_CONST=0x11, OP_PUSH_NIL=0x12,
    OP_PUSH_TRUE=0x13, OP_PUSH_FALSE=0x14, OP_POP=0x15, OP_DUP=0x16, OP_SWAP=0x17,
    OP_LOAD_LOCAL=0x20, OP_STORE_LOCAL=0x21, OP_LOAD_GLOBAL=0x22, OP_STORE_GLOBAL=0x23,
    OP_ADD=0x30, OP_SUB=0x31, OP_MUL=0x32, OP_DIV=0x33, OP_MOD=0x34, OP_NEG=0x35,
    OP_ADD_NUM=0xC0, OP_SUB_NUM=0xC1, OP_MUL_NUM=0xC2, OP_DIV_NUM=0xC3, OP_MOD_NUM=0xC4,
    OP_LT_NUM=0xC5, OP_LE_NUM=0xC6, OP_GT_NUM=0xC7, OP_GE_NUM=0xC8,
    OP_EQ_NUM=0xC9, OP_NE_NUM=0xCA,
    OP_ADD_SLOT_IMM=0xD0, OP_ADD_SLOT_SLOT=0xD1, OP_LT_SLOT_IMM=0xD2,
    OP_MAKE_FUNC=0xD3, OP_CALL_VALUE=0xD4, OP_HIGHER_ORDER=0xD5,
    OP_SLOT_INDEX_SLOT=0xD6, OP_SLOT_INDEX_IMM=0xD7, OP_SLOT_INDEX_STORE=0xD8,
    OP_SUB_SLOT_IMM=0xD9, OP_MUL_SLOT_IMM=0xDA, OP_DIV_SLOT_IMM=0xDB, OP_MOD_SLOT_IMM=0xDC, OP_SUB_SLOT_SLOT=0xDD, OP_MUL_SLOT_SLOT=0xDE, OP_GT_SLOT_IMM=0xDF, OP_LE_SLOT_IMM=0xE0, OP_GE_SLOT_IMM=0xE1, OP_EQ_SLOT_IMM=0xE2, OP_NE_SLOT_IMM=0xE3, OP_SLOT_APPEND_SLOT=0xE4, OP_SLOT_APPEND_IMM=0xE5, OP_SLOT_CONCAT_SLOT=0xE6, OP_SLOT_APPEND_STACK=0xE7, OP_MAKE_CLOSURE=0xE8, OP_LOAD_FREE=0xE9, OP_FIELD_SET=0xEA,
    OP_CONCAT=0x36,
    OP_EQ=0x40, OP_NE=0x41, OP_LT=0x42, OP_LE=0x43, OP_GT=0x44, OP_GE=0x45, OP_NOT=0x46,
    OP_JMP=0x50, OP_JMP_IF=0x51, OP_JMP_IFNOT=0x52,
    OP_CALL=0x60, OP_RET=0x61, OP_RET_NIL=0x62,
    OP_MAKE_SEQ=0x70, OP_MAKE_MAP=0x71, OP_MAKE_REC=0x72, OP_FIELD_GET=0x73,
    OP_INDEX_GET=0x74, OP_INDEX_SET=0x75, OP_LEN=0x76,
    OP_PRINT=0x80, OP_THROW=0x81,
    OP_CAP_CHECK=0x90, OP_SANDBOX_ENTER=0x91, OP_SANDBOX_EXIT=0x92, OP_AUTHORIZE=0x93,
    OP_TRY_BEGIN=0xA0, OP_TRY_END=0xA1, OP_END_TRY=0xA2,
    OP_BUILTIN=0xB0, OP_CALL_STD=0xB1, OP_CALL_HOST=0xB2, OP_CALL_TOOL=0xB3, OP_CALL_FFI=0xB9,
    OP_SPAWN=0xB4, OP_YIELD=0xB5, OP_RECV=0xB6, OP_JOIN=0xB7, OP_AWAIT=0xB8
};

/* ============ 运行时状态 ============ */

#define MAX_SLOTS  (1 << 20)
#define MAX_STACK  (1 << 20)
#define MAX_FRAMES (1 << 16)

static Val     g_slots[MAX_SLOTS];
static int     g_slot_top = 0;

static Val     g_stack[MAX_STACK];
static int     g_stack_top = 0;

static uint32_t g_ffunc[MAX_FRAMES], g_fbase[MAX_FRAMES], g_fret[MAX_FRAMES];
/* 每个帧的值栈基址：函数内 sp 从这里开始，返回时恢复到调用前 */
static int      g_fsp[MAX_FRAMES];
/* 保存调用者的代码指针与长度：返回时直接恢复，避免用 g_ffunc 反推
   （入口帧的 -1 哨兵值曾导致 code 恢复错误——真实 bug 根因） */
static const uint8_t *g_fcode[MAX_FRAMES];
static uint32_t       g_fcode_len[MAX_FRAMES];
static int     g_frame_top = 0;

static Val    *g_globals;

/* ============ 并发：任务与调度 ============ */

/* 每个任务持有完整的执行上下文：值栈、帧栈、槽位区都归自己所有。
   这是「无数据竞争」的前提——任务之间不共享任何可变执行状态。 */

#define MAX_TASKS 256

typedef struct Task {
    pthread_t     thread;
    int           used;
    int           done;                      /* 已完成（成功或失败） */
    int           group;                     /* 所属并发块 id */
    /* 执行上下文（每任务独立分配） */
    Val          *slots;   int slot_top;
    Val          *stack;   int sp;
    uint32_t     *ffunc, *fbase, *fret;
    int          *fsp;
    const uint8_t **fcode;
    uint32_t     *fcode_len;
    int           frame_top;
    const uint8_t *code;   uint32_t code_len;
    int           pc;
    /* 本任务发起的子任务：JOIN 时等待它们全部完成。
       用动态数组避免 Task 结构过大（否则主任务在栈上放不下）。 */
    struct Task **children;
    int           n_children;
    int           cap_children;
    int           shared_slots;   /* 1 = 槽位区与外层共享（lambda 用） */
    /* 信箱名：任务结束时返回值写入这里（= 做法名_信箱） */
    char          mailbox[128];
    int           mailbox_slot;   /* 父任务中接收该信箱的槽位（-1 表示按名查找） */
    /* 结果与错误 */
    Val           result;
    int           failed;
    char          err_cat[64];
    char          err_msg[1024];
} Task;

/* 信箱：每个任务名一个信箱，按完成顺序排队 */
#define MAX_MAILBOX 64
typedef struct {
    char  name[128];
    Val   items[64];
    int   n;
} Mailbox;

static Mailbox  g_mailboxes[MAX_MAILBOX];
static int      g_mailbox_n = 0;
static pthread_mutex_t g_mb_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_mb_cond = PTHREAD_COND_INITIALIZER;

/* 任务组：用于 同时 块尾 JOIN */
typedef struct {
    int group;
    int pending;
} GroupWait;
static GroupWait g_groups[MAX_TASKS];
static int       g_group_n = 0;

static Task *g_tasks[MAX_TASKS];
static pthread_mutex_t g_task_lock = PTHREAD_MUTEX_INITIALIZER;

/* 输出格式化 */

static void fmt_val(Val v, char *buf, size_t cap, int depth);

static void fmt_num(double d, char *buf, size_t cap) {
    if (d == (double)(long long)d && fabs(d) < 1e18) {
        snprintf(buf, cap, "%lld", (long long)d);
        return;
    }
    /* 与既有示例输出对齐：6 位小数后裁掉尾随零。
       （圆周率等常量由调用方用 fmt_num_hi 另行格式化。） */
    char tmp[64];
    snprintf(tmp, sizeof tmp, "%.6f", d);
    size_t n = strlen(tmp);
    while (n > 0 && tmp[n - 1] == '0') n--;
    if (n > 0 && tmp[n - 1] == '.') n--;
    tmp[n] = 0;
    snprintf(buf, cap, "%s", tmp);
}

/* 高精度格式：用于数学常量等需要保留有效数字的场景 */
static void fmt_num_hi(double d, char *buf, size_t cap) {
    snprintf(buf, cap, "%.15g", d);
}

static void fmt_val(Val v, char *buf, size_t cap, int depth) {
    if (depth > 8) { snprintf(buf, cap, "..."); return; }
    switch (v.tag) {
    case T_NIL:  snprintf(buf, cap, "空"); break;
    case T_BOOL: snprintf(buf, cap, "%s", v.u.b ? "真" : "假"); break;
    case T_NUM:  fmt_num(v.u.num, buf, cap); break;
    case T_TEXT: snprintf(buf, cap, "%s", v.u.obj->text); break;
    case T_SEQ: {
        size_t used = 0;
        used += (size_t)snprintf(buf + used, cap - used, "[");
        for (uint32_t i = 0; i < v.u.obj->n && used < cap; i++) {
            char tmp[512];
            fmt_val(v.u.obj->items[i], tmp, sizeof tmp, depth + 1);
            used += (size_t)snprintf(buf + used, cap - used,
                                     "%s%s", i ? ", " : "", tmp);
        }
        snprintf(buf + (used < cap ? used : cap - 1), cap - (used < cap ? used : cap - 1), "]");
        break;
    }
    case T_MAP:
    case T_REC: {
        size_t used = 0;
        Obj *o = v.u.obj;
        if (v.tag == T_REC && o->rec_name)
            used += (size_t)snprintf(buf + used, cap - used, "%s(", o->rec_name);
        else
            used += (size_t)snprintf(buf + used, cap - used, "{");
        for (uint32_t i = 0; i < o->n && used < cap; i++) {
            char tmp[512];
            fmt_val(o->vals[i], tmp, sizeof tmp, depth + 1);
            if (v.tag == T_REC)
                used += (size_t)snprintf(buf + used, cap - used,
                                         "%s%s=%s", i ? ", " : "", o->keys[i], tmp);
            else
                used += (size_t)snprintf(buf + used, cap - used,
                                         "%s%s: %s", i ? ", " : "", o->keys[i], tmp);
        }
        if (used < cap)
            snprintf(buf + used, cap - used, "%s", v.tag == T_REC ? ")" : "}");
        break;
    }
    default: snprintf(buf, cap, "?"); break;
    }
}

/* 文本化（用于拼接） */
static void to_text(Val v, char *buf, size_t cap) {
    if (v.tag == T_TEXT) { snprintf(buf, cap, "%s", v.u.obj->text); return; }
    fmt_val(v, buf, cap, 0);
}

/* ============ 堆辅助 ============ */

static Obj *seq_new(void) { return obj_new(T_SEQ); }

static void seq_push(Obj *s, Val v) {
    uint32_t n = s->n;
    if ((n & (n - 1)) == 0 && n >= 2) {  /* 按 2 的幂扩容 */
        Val *p = (Val *)realloc(s->items, sizeof(Val) * n * 2);
        if (!p) raise_err("越界", "序列扩容失败");
        s->items = p;
    }
    if (!s->items) s->items = (Val *)malloc(sizeof(Val) * 4);
    s->items[n] = v;
    s->n = n + 1;
}

/* 写入映射。
   所有权约定：map 自己持有值的引用，因此这里必须 retain。
   曾经漏掉 retain：调用方（如 INDEX_SET）随后释放自己的那份引用，
   映射里就留下悬垂指针，取回时 SIGSEGV——序列存进映射即触发。 */
/* FNV-1a 字符串哈希 */
static uint32_t str_hash(const char *s) {
    uint32_t h = 2166136261u;
    while (*s) { h ^= (uint8_t)*s++; h *= 16777619u; }
    return h;
}

static void map_rehash(Obj *m, uint32_t want) {
    uint32_t nb = 8;
    while (nb < want * 2) nb <<= 1;
    free(m->buckets);
    m->buckets = (int32_t *)malloc(sizeof(int32_t) * nb);
    for (uint32_t i = 0; i < nb; i++) m->buckets[i] = -1;
    m->nbuckets = nb;
    for (uint32_t i = 0; i < m->n; i++) {
        uint32_t b = str_hash(m->keys[i]) & (nb - 1);
        m->next[i] = m->buckets[b];
        m->buckets[b] = (int32_t)i;
    }
}

/* 查键返回条目下标，未找到返回 -1 */
static int32_t map_find(Obj *m, const char *key) {
    if (!m->buckets || m->nbuckets == 0) return -1;
    uint32_t b = str_hash(key) & (m->nbuckets - 1);
    for (int32_t i = m->buckets[b]; i >= 0; i = m->next[i])
        if (strcmp(m->keys[i], key) == 0) return i;
    return -1;
}

static void map_set(Obj *m, const char *key, Val v) {
    val_retain(v);
    int32_t hit = map_find(m, key);
    if (hit >= 0) {
        val_release(m->vals[hit]);
        m->vals[hit] = v;
        return;
    }
    if (m->n == m->cap) {
        m->cap = m->cap ? m->cap * 2 : 8;
        m->keys = (char **)realloc(m->keys, sizeof(char *) * m->cap);
        m->vals = (Val *)realloc(m->vals, sizeof(Val) * m->cap);
        m->next = (int32_t *)realloc(m->next, sizeof(int32_t) * m->cap);
    }
    if (!m->buckets || m->n + 1 > m->nbuckets * 3 / 4) map_rehash(m, m->n + 1);
    uint32_t idx = m->n;
    m->keys[idx] = strdup(key);
    m->vals[idx] = v;
    uint32_t b = str_hash(key) & (m->nbuckets - 1);
    m->next[idx] = m->buckets[b];
    m->buckets[b] = (int32_t)idx;
    m->n++;
}

static Val map_get(Obj *m, const char *key) {
    int32_t i = map_find(m, key);
    return i >= 0 ? m->vals[i] : v_nil();
}

/* ============ 执行 ============ */

static void print_val(Val v) {
    char buf[4096];
    fmt_val(v, buf, sizeof buf, 0);
    fputs(buf, stdout);
    fputc('\n', stdout);
}

static inline uint16_t rd16(const uint8_t *c, int *p) {
    uint16_t v; memcpy(&v, c + *p, 2); *p += 2; return v;
}
static inline uint32_t rd32(const uint8_t *c, int *p) {
    uint32_t v; memcpy(&v, c + *p, 4); *p += 4; return v;
}
static inline int32_t rd_i32(const uint8_t *c, int *p) {
    int32_t v; memcpy(&v, c + *p, 4); *p += 4; return v;
}
static inline double rd_d(const uint8_t *c, int *p) {
    double v; memcpy(&v, c + *p, 8); *p += 8; return v;
}

/* 把「相对下一条指令的字节偏移」换算成「指令下标」。
   与 Python 端解码器保持一致：TRY_BEGIN 的目标必须是指令下标，
   否则会把字节偏移当下标用，跳到指令中间（真实 bug 的根因）。 */
/* 把「相对下一条指令」的偏移换算成绝对字节偏移。
   C 端 pc 始终是字节偏移（与 Python 端的指令下标不同），
   所以这里只做一次加法，不需要换算成指令序号。 */
static int jump_target(int insn_pc, int insn_width, int32_t rel) {
    return insn_pc + insn_width + rel;
}

/* ---- 任务与信箱辅助 ---- */

static void *task_thread(void *arg);
static Val val_clone(Val v);

/* 登记子任务：JOIN 时要等这些任务全部结束 */
static void task_add_child(Task *parent, Task *child) {
    if (parent->n_children == parent->cap_children) {
        int cap = parent->cap_children ? parent->cap_children * 2 : 8;
        Task **p = (Task **)realloc(parent->children, sizeof(Task *) * cap);
        if (!p) return;
        parent->children = p;
        parent->cap_children = cap;
    }
    parent->children[parent->n_children++] = child;
}

static Task *task_create(uint16_t fi, Val *args, uint8_t nargs, int group) {
    Task *t = (Task *)calloc(1, sizeof(Task));
    if (!t) return NULL;
    Func *fn = &g_funcs[fi];
    int nslots = fn->n_slots > nargs ? fn->n_slots : nargs;
    t->slots = (Val *)calloc(nslots ? nslots : 1, sizeof(Val));
    t->stack = (Val *)calloc(1024, sizeof(Val));
    t->ffunc = (uint32_t *)calloc(64, sizeof(uint32_t));
    t->fbase = (uint32_t *)calloc(64, sizeof(uint32_t));
    t->fret  = (uint32_t *)calloc(64, sizeof(uint32_t));
    t->fsp   = (int *)calloc(64, sizeof(int));
    t->fcode = (const uint8_t **)calloc(64, sizeof(uint8_t *));
    t->fcode_len = (uint32_t *)calloc(64, sizeof(uint32_t));
    t->slot_top = 0;
    t->sp = 0;
    t->frame_top = 0;
    t->code = fn->code;
    t->code_len = fn->code_len;
    t->pc = 0;
    t->group = group;
    t->result = v_nil();
    /* 参数深拷贝进子任务：跨任务只传值，不共享对象 */
    for (int i = 0; i < nargs; i++) t->slots[i] = val_clone(args[i]);

    t->slot_top = nslots;
    t->used = 1;
    /* 建立子任务的入口帧：base=0，返回地址 -1 表示「返回即任务结束」。
       缺了这一步 frame_top 会从 0 开始，LOAD_LOCAL 里 base[-1] 直接越界。 */
    t->ffunc[0] = fi;
    t->fbase[0] = 0;
    t->fret[0] = 0;
    t->fsp[0] = 0;
    t->fcode[0] = fn->code;
    t->fcode_len[0] = fn->code_len;
    t->frame_top = 1;
    pthread_mutex_lock(&g_task_lock);
    for (int i = 0; i < MAX_TASKS; i++) {
        if (!g_tasks[i]) { g_tasks[i] = t; break; }
    }
    pthread_mutex_unlock(&g_task_lock);
    return t;
}

static void val_copy_to(Val v, Val *out);

/* 深拷贝：跨任务传递只走「值」，绝不共享堆对象指针。
   这是无数据竞争的根本保证。 */
static Val val_clone(Val v) {
    Val out;
    memset(&out, 0, sizeof out);
    out.tag = v.tag;
    switch (v.tag) {
    case T_NUM:  out.u.num = v.u.num; break;
    case T_BOOL: out.u.b = v.u.b; break;
    case T_NIL:  break;
    case T_TEXT:
        out = v_text_n(v.u.obj->text, v.u.obj->len);
        break;
    case T_SEQ: {
        Obj *o = seq_new();
        o->items = (Val *)calloc(v.u.obj->n ? v.u.obj->n : 1, sizeof(Val));
        o->n = v.u.obj->n;
        for (uint32_t i = 0; i < v.u.obj->n; i++) o->items[i] = val_clone(v.u.obj->items[i]);
        out = v_obj(o);
        break;
    }
    case T_MAP:
    case T_REC: {
        Obj *o = obj_new(v.tag);
        o->rec_name = v.u.obj->rec_name;
        for (uint32_t i = 0; i < v.u.obj->n; i++)
            map_set(o, v.u.obj->keys[i], val_clone(v.u.obj->vals[i]));
        out = v_obj(o);
        break;
    }
    default: break;
    }
    return out;
}

static void val_copy_to(Val v, Val *out) { *out = val_clone(v); }

/* 信箱：按名字找（不存在则建） */
static Mailbox *mailbox_get(const char *name, int create) {
    for (int i = 0; i < g_mailbox_n; i++)
        if (strcmp(g_mailboxes[i].name, name) == 0) return &g_mailboxes[i];
    if (!create || g_mailbox_n >= MAX_MAILBOX) return NULL;
    Mailbox *m = &g_mailboxes[g_mailbox_n++];
    snprintf(m->name, sizeof m->name, "%s", name);
    m->n = 0;
    return m;
}

/* 任务结束：把返回值深拷贝进信箱，并通知等待者 */
static void mailbox_push_task(Task *t) {
    pthread_mutex_lock(&g_mb_lock);
    Mailbox *m = mailbox_get(t->mailbox, 1);
    if (m && m->n < 64) m->items[m->n++] = t->result;
    pthread_cond_broadcast(&g_mb_cond);
    pthread_mutex_unlock(&g_mb_lock);
}

/* 取信箱：没有就等待 */
static Val mailbox_take(const char *name) {
    pthread_mutex_lock(&g_mb_lock);
    Mailbox *m = mailbox_get(name, 1);
    while (!m || m->n == 0) {
        pthread_cond_wait(&g_mb_cond, &g_mb_lock);
        m = mailbox_get(name, 1);
    }
    Val v = m->items[0];
    for (int i = 1; i < m->n; i++) m->items[i - 1] = m->items[i];
    m->n--;
    pthread_mutex_unlock(&g_mb_lock);
    return v;
}

/* 任务组等待：等本组发起数全部完成 */
static void group_wait(int group, int n) {
    if (group < 0 || n <= 0) return;
    while (1) {
        int pending = 0;
        pthread_mutex_lock(&g_task_lock);
        for (int i = 0; i < MAX_TASKS; i++)
            if (g_tasks[i] && g_tasks[i]->group == group && !g_tasks[i]->done) pending++;
        pthread_mutex_unlock(&g_task_lock);
        if (pending == 0) break;
        sched_yield();
    }
}

/* ============================================================
   FFI：直接调用 C 动态库（dlopen / dlsym）
   不依赖任何宿主语言——这是原生 VM 该有的 FFI 形态。

   类型映射（约定，够覆盖绝大多数 C 库）：
     衡语 数   → double；若目标函数是整型接口，按 long 传（见 ffi_kind）
     衡语 文本 → const char*（UTF-8）
     衡语 表   → 仅支持元素为数的序列，展开成指针（预留）
     返回值     → 数（double）或 文本（char*，自动拷贝）

   由于 C 无反射，函数原型由「名字约定」或显式声明给出。
   为保持简单可靠，这里支持两类签名：
     R(double, double, ...) → double      （数学库绝大多数）
     R(const char*)         → const char* （字符串处理类）
   ============================================================ */

#include <dlfcn.h>

/* 取自身可执行文件路径。
   macOS 用 _NSGetExecutablePath（Apple 专有），Linux 读 /proc/self/exe。
   早期直接调用 _NSGetExecutablePath，导致 Linux 上链接失败：
     undefined reference to `_NSGetExecutablePath'
   而 VM 本身是纯 C、本该跨平台，不该被一个路径查询函数卡住。 */
static void heng_self_path(char *out, size_t cap) {
    if (!out || cap == 0) return;
    out[0] = 0;
#ifdef __APPLE__
    uint32_t sz = (uint32_t)cap;
    extern int _NSGetExecutablePath(char *, uint32_t *);
    if (_NSGetExecutablePath(out, &sz) != 0) out[0] = 0;
#elif defined(__linux__)
    ssize_t n = readlink("/proc/self/exe", out, cap - 1);
    if (n > 0) out[n] = 0; else out[0] = 0;
#else
    /* 其他平台尽力而为：留空，调用方会退回 getenv("HENG_SELF") */
    out[0] = 0;
#endif
}

typedef double (*ffi_dd)(double, double);
typedef double (*ffi_d)(double);
typedef double (*ffi_d0)(void);
typedef char *(*ffi_ss)(const char *);
typedef const char *(*ffi_cs)(const char *, const char *);
typedef const char *(*ffi_cs1)(const char *);      /* 单文本入 → 文本出 */
typedef int (*ffi_i2)(int, int);

/* FfiLib 与全局表在文件前部声明（load_module 需要用到） */

/* 库名候选：不同平台的命名约定不同。
   macOS 上 libm 并入 libSystem，必须做别名映射，否则 dlopen 直接失败。 */
static void *ffi_try_open(const char *lib) {
    void *h = dlopen(lib, RTLD_LAZY | RTLD_LOCAL);
    if (h) return h;

    /* 相对路径：按「脚本所在目录」再试一次。
       否则 `导入 外部 "libhengui.dylib"` 会依赖当前工作目录，
       在 IDE 目录之外启动就找不到库——这正是第一次跑 IDE 失败的原因。 */
    if (lib && lib[0] != '/') {
        const char *sp = getenv("HENG_SCRIPT");
        char cand[1024];
        if (sp && *sp) {
            snprintf(cand, sizeof cand, "%s", sp);
            char *slash = strrchr(cand, '/');
            if (slash) {
                *slash = 0;
                char full[1200];
                snprintf(full, sizeof full, "%s/%s", cand, lib);
                h = dlopen(full, RTLD_LAZY | RTLD_LOCAL);
                if (h) return h;
            }
        }
    }

    char buf[256];
    /* 补 lib 前缀与平台后缀 */
    const char *sfx[] = {
#ifdef __APPLE__
        ".dylib", ".so", "",
#else
        ".so", ".so.6", "",
#endif
    };
    for (size_t i = 0; i < sizeof sfx / sizeof sfx[0]; i++) {
        if (!strncmp(lib, "lib", 3)) snprintf(buf, sizeof buf, "%s%s", lib, sfx[i]);
        else                          snprintf(buf, sizeof buf, "lib%s%s", lib, sfx[i]);
        h = dlopen(buf, RTLD_LAZY | RTLD_LOCAL);
        if (h) return h;
    }

#ifdef __APPLE__
    /* macOS：数学函数在 libSystem 里 */
    if (strcmp(lib, "libm") == 0 || strcmp(lib, "m") == 0) {
        h = dlopen("/usr/lib/libSystem.dylib", RTLD_LAZY | RTLD_LOCAL);
        if (h) return h;
    }
#else
    /* Linux：数学函数在 libm.so.6 */
    if (strcmp(lib, "libm") == 0 || strcmp(lib, "m") == 0) {
        h = dlopen("libm.so.6", RTLD_LAZY | RTLD_LOCAL);
        if (h) return h;
    }
#endif

    /* 最后再试主程序自身的符号（RTLD_DEFAULT） */
    if (dlsym(RTLD_DEFAULT, "sqrt")) return RTLD_DEFAULT;
    return NULL;
}

static void *ffi_open(const char *alias) {
    for (int i = 0; i < g_ffi_n; i++)
        if (strcmp(g_ffi_libs[i].alias, alias) == 0) {
            if (g_ffi_libs[i].handle) return g_ffi_libs[i].handle;
            void *h = ffi_try_open(g_ffi_libs[i].lib);
            g_ffi_libs[i].handle = h;
            return h;
        }
    return NULL;
}

/* 按实参个数调用：常见签名直接转换 */
static Val ffi_call(const char *full_name, Val *args, int n) {
    /* full_name 形如「数学.sqrt」：切出别名与符号名 */
    char alias[64] = "", sym[128] = "";
    const char *dot = strchr(full_name, '.');
    if (dot) {
        size_t al = (size_t)(dot - full_name);
        if (al >= sizeof alias) al = sizeof alias - 1;
        memcpy(alias, full_name, al); alias[al] = 0;
        snprintf(sym, sizeof sym, "%s", dot + 1);
    } else {
        snprintf(sym, sizeof sym, "%s", full_name);
    }

    if (g_trace) {
        fprintf(stderr, "[ffi] full=%s alias=%s sym=%s n_libs=%d\n", full_name, alias, sym, g_ffi_n);
        for (int i = 0; i < g_ffi_n; i++)
            fprintf(stderr, "  lib[%d] alias='%s' lib='%s'\n", i, g_ffi_libs[i].alias, g_ffi_libs[i].lib);
    }
    void *h = ffi_open(alias);
    if (!h) {
        char msg[256];
        snprintf(msg, sizeof msg, "无法加载外部库「%s」（请用 导入 外部 \"库名\" 之 %s 声明）",
                 alias, alias);
        raise_err("未授权", msg);
    }
    void *fp = dlsym(h, sym);
    if (!fp) {
        char msg[256];
        snprintf(msg, sizeof msg, "外部库中没有符号「%s」", sym);
        raise_err("未定义", msg);
    }

    /* 约定：符号名以 _t 结尾 → 返回 const char*（文本）。
       没有这个约定时，「一个文本参数」的函数无法区分
       「返回字符串」与「返回整数」——两者在 ABI 的返回寄存器不同，
       按错的方式解释会拿到野指针，直接段错误。
       加后缀是显式声明，比猜返回类型可靠。 */
    int ret_text = (strlen(sym) > 2 && strcmp(sym + strlen(sym) - 2, "_t") == 0);

    /* 逐个尝试常见签名：先用 double 路径，类型不符时给出明确错误 */
    double da[8] = {0};
    int all_num = 1;
    for (int i = 0; i < n && i < 8; i++) {
        if (args[i].tag == T_NUM) da[i] = args[i].u.num;
        else { all_num = 0; break; }
    }

    if (all_num) {
        switch (n) {
        case 0:
            if (ret_text) {
                const char *r0 = ((const char *(*)(void))fp)();
                return r0 ? v_text(r0) : v_text("");
            }
            return v_num(((ffi_d0)fp)());
        case 1: return v_num(((ffi_d)fp)(da[0]));
        case 2: return v_num(((ffi_dd)fp)(da[0], da[1]));
        default: {
            /* 更多参数：统一走 double 传参（ABI 对浮点参数一致） */
            double (*f8)(double,double,double,double,double,double,double,double) =
                (double (*)(double,double,double,double,double,double,double,double))fp;
            while (n < 8) da[n++] = 0;
            return v_num(f8(da[0],da[1],da[2],da[3],da[4],da[5],da[6],da[7]));
        }
        }
    }

    /* 字符串路径 */
    if (n == 1 && args[0].tag == T_TEXT) {
        if (ret_text) {
            const char *r = ((ffi_cs1)fp)(args[0].u.obj->text);
            return r ? v_text(r) : v_text("");
        }
        char *r = ((ffi_ss)fp)(args[0].u.obj->text);
        return r ? v_text(r) : v_nil();
    }
    if (n == 2 && args[0].tag == T_TEXT && args[1].tag == T_TEXT) {
        const char *r = ((ffi_cs)fp)(args[0].u.obj->text, args[1].u.obj->text);
        return r ? v_text(r) : v_nil();
    }

    char msg[256];
    snprintf(msg, sizeof msg, "外部函数「%s」的参数类型不受支持（当前支持：全数值 / 单文本 / 双文本）", sym);
    raise_err("类型", msg);
    return v_nil();
}

/* ============================================================
   HTTP 客户端（零依赖，BSD socket）
   只支持 http:// 与 https:// 的 GET/POST。
   HTTPS 若系统有 libssl 则可用，否则明确报错——绝不静默降级。
   ============================================================ */

#include <sys/socket.h>
#include <termios.h>
#include <netdb.h>
#include <unistd.h>

static int http_parse_url(const char *url, char *host, size_t hostcap,
                          char *path, size_t pathcap, int *port, int *tls) {
    *tls = 0; *port = 80;
    const char *p = url;
    if (!strncmp(p, "https://", 8)) { *tls = 1; *port = 443; p += 8; }
    else if (!strncmp(p, "http://", 7)) { p += 7; }
    else return 0;
    const char *slash = strchr(p, '/');
    const char *colon = strchr(p, ':');
    size_t hlen;
    if (colon && (!slash || colon < slash)) {
        hlen = (size_t)(colon - p);
        *port = atoi(colon + 1);
    } else {
        hlen = slash ? (size_t)(slash - p) : strlen(p);
    }
    if (hlen == 0 || hlen >= hostcap) return 0;
    memcpy(host, p, hlen); host[hlen] = 0;
    snprintf(path, pathcap, "%s", slash ? slash : "/");
    return 1;
}

/* 把响应体写进 out；返回 HTTP 状态码，失败返回 -1 */
static int http_request(const char *url, const char *method, const char *payload,
                        char **out_body) {
    char host[256], path[1024];
    int port, tls;
    *out_body = NULL;
    if (!http_parse_url(url, host, sizeof host, path, sizeof path, &port, &tls)) {
        runtime_error("网络", "URL 必须形如 http://主机/路径");
        return -1;
    }
    if (tls) {
        runtime_error("网络", "原生 VM 的 HTTPS 需要系统 libssl（当前未链接）");
        return -1;
    }
    char portstr[16];
    snprintf(portstr, sizeof portstr, "%d", port);

    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, portstr, &hints, &res) != 0) {
        runtime_error("网络", "域名解析失败");
        return -1;
    }
    int fd = -1;
    for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) break;
        close(fd); fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) { runtime_error("网络", "连接失败"); return -1; }

    size_t plen = payload ? strlen(payload) : 0;
    char *req = (char *)malloc(plen + 1024);
    int rl = snprintf(req, plen + 1024,
        "%s %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: Heng/2\r\n"
        "Connection: close\r\nAccept: */*\r\n"
        "Content-Type: application/json\r\nContent-Length: %zu\r\n\r\n%s",
        method, path, host, plen, payload ? payload : "");
    size_t sent = 0;
    while (sent < (size_t)rl) {
        ssize_t w = write(fd, req + sent, (size_t)rl - sent);
        if (w <= 0) { free(req); close(fd); runtime_error("网络", "发送失败"); return -1; }
        sent += (size_t)w;
    }
    free(req);

    size_t cap = 8192, used = 0;
    char *resp = (char *)malloc(cap);
    ssize_t got;
    while ((got = read(fd, resp + used, cap - used - 1)) > 0) {
        used += (size_t)got;
        if (used + 1 >= cap) { cap *= 2; resp = (char *)realloc(resp, cap); }
    }
    close(fd);
    resp[used] = 0;

    int status = 0;
    if (!strncmp(resp, "HTTP/1.", 7)) status = atoi(resp + 9);
    char *body = strstr(resp, "\r\n\r\n");
    if (body) body += 4; else body = resp;

    /* 如果是 chunked，简单去除分块头（够用于常见 API） */
    if (strstr(resp, "Transfer-Encoding: chunked")) {
        char *dec = (char *)malloc(strlen(body) + 1);
        size_t dn = 0;
        char *p = body;
        while (*p) {
            char *end = NULL;
            long clen = strtol(p, &end, 16);
            if (clen <= 0 || end == p) break;
            p = end;
            while (*p == '\r' || *p == '\n') p++;
            memcpy(dec + dn, p, (size_t)clen);
            dn += (size_t)clen;
            p += clen;
            while (*p == '\r' || *p == '\n') p++;
        }
        dec[dn] = 0;
        *out_body = dec;
        free(resp);
    } else {
        *out_body = strdup(body);
        free(resp);
    }
    return status;
}

static Val http_call(const char *name, Val *args, int n) {
    const char *url = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
    int is_get = (strstr(name, "取") != NULL);
    char *payload = NULL;
    if (!is_get && n >= 2 && args[1].tag == T_TEXT) payload = args[1].u.obj->text;
    char *body = NULL;
    int status = http_request(url, is_get ? "GET" : "POST", payload, &body);
    Obj *o = obj_new(T_MAP);
    map_set(o, "状态", v_num((double)status));
    map_set(o, "正文", v_text(body ? body : ""));
    free(body);
    return v_obj(o);
}

/* ============================================================
   JSON 编解码（零依赖，自成一体）
   与 Python 版 json.dumps(ensure_ascii=False) / json.loads 语义对齐。
   ============================================================ */

typedef struct { char *buf; size_t cap, used; } JBuf;

static void jb_init(JBuf *b) { b->cap = 256; b->buf = (char *)malloc(b->cap); b->used = 0; b->buf[0] = 0; }
static void jb_need(JBuf *b, size_t extra) {
    if (b->used + extra + 1 <= b->cap) return;
    while (b->used + extra + 1 > b->cap) b->cap *= 2;
    b->buf = (char *)realloc(b->buf, b->cap);
}
static void jb_putc(JBuf *b, char c) { jb_need(b, 1); b->buf[b->used++] = c; b->buf[b->used] = 0; }
static void jb_puts(JBuf *b, const char *s) {
    size_t n = strlen(s); jb_need(b, n); memcpy(b->buf + b->used, s, n);
    b->used += n; b->buf[b->used] = 0;
}

static void json_escape(JBuf *b, const char *s, uint32_t len) {
    jb_putc(b, '"');
    for (uint32_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '"':  jb_puts(b, "\\\""); break;
        case '\\': jb_puts(b, "\\\\"); break;
        case '\n': jb_puts(b, "\\n");  break;
        case '\r': jb_puts(b, "\\r");  break;
        case '\t': jb_puts(b, "\\t");  break;
        case '\b': jb_puts(b, "\\b");  break;
        case '\f': jb_puts(b, "\\f");  break;
        default:
            if (c < 0x20) {
                char tmp[8]; snprintf(tmp, sizeof tmp, "\\u%04x", c); jb_puts(b, tmp);
            } else {
                jb_putc(b, (char)c);   /* UTF-8 原样输出（ensure_ascii=False） */
            }
        }
    }
    jb_putc(b, '"');
}

static void json_encode_into(JBuf *b, Val v, int indent, int depth) {
    char tmp[64];
    switch (v.tag) {
    case T_NIL:
        jb_puts(b, "null"); break;
    case T_BOOL:
        jb_puts(b, v.u.b ? "true" : "false"); break;
    case T_NUM:
        if (v.u.num != v.u.num || v.u.num > 1e308 || v.u.num < -1e308) jb_puts(b, "null");
        else if (v.u.num == (double)(long long)v.u.num && fabs(v.u.num) < 1e15)
            { snprintf(tmp, sizeof tmp, "%lld", (long long)v.u.num); jb_puts(b, tmp); }
        else { snprintf(tmp, sizeof tmp, "%.17g", v.u.num); jb_puts(b, tmp); }
        break;
    case T_TEXT:
        json_escape(b, v.u.obj->text, v.u.obj->len);
        break;
    case T_SEQ: {
        jb_putc(b, '[');
        for (uint32_t i = 0; i < v.u.obj->n; i++) {
            if (i) jb_putc(b, ',');
            if (indent > 0) {
                jb_putc(b, '\n');
                for (int k = 0; k < (depth + 1) * indent; k++) jb_putc(b, ' ');
            }
            json_encode_into(b, v.u.obj->items[i], indent, depth + 1);
        }
        if (indent > 0 && v.u.obj->n) {
            jb_putc(b, '\n');
            for (int k = 0; k < depth * indent; k++) jb_putc(b, ' ');
        }
        jb_putc(b, ']');
        break;
    }
    case T_MAP: case T_REC: {
        jb_putc(b, '{');
        for (uint32_t i = 0; i < v.u.obj->n; i++) {
            if (i) jb_putc(b, ',');
            if (indent > 0) {
                jb_putc(b, '\n');
                for (int k = 0; k < (depth + 1) * indent; k++) jb_putc(b, ' ');
            }
            json_escape(b, v.u.obj->keys[i], (uint32_t)strlen(v.u.obj->keys[i]));
            jb_putc(b, ':');
            if (indent > 0) jb_putc(b, ' ');
            json_encode_into(b, v.u.obj->vals[i], indent, depth + 1);
        }
        if (indent > 0 && v.u.obj->n) {
            jb_putc(b, '\n');
            for (int k = 0; k < depth * indent; k++) jb_putc(b, ' ');
        }
        jb_putc(b, '}');
        break;
    }
    default:
        jb_puts(b, "null");
    }
}

static char *json_encode_val(Val v, int indent) {
    JBuf b; jb_init(&b);
    json_encode_into(&b, v, indent, 0);
    return b.buf;
}

/* ---- 解码 ---- */

typedef struct { const char *s; size_t i, n; int failed; } JParser;

static void jp_ws(JParser *p) {
    while (p->i < p->n && (p->s[p->i] == ' ' || p->s[p->i] == '\t' ||
                           p->s[p->i] == '\n' || p->s[p->i] == '\r')) p->i++;
}

static Val jp_value(JParser *p);

static Val jp_string(JParser *p) {
    if (p->i >= p->n || p->s[p->i] != '"') { p->failed = 1; return v_nil(); }
    p->i++;
    size_t cap = 32, used = 0;
    char *buf = (char *)malloc(cap);
    while (p->i < p->n && p->s[p->i] != '"') {
        char c = p->s[p->i++];
        if (c == '\\' && p->i < p->n) {
            char e = p->s[p->i++];
            switch (e) {
            case 'n': c = '\n'; break;
            case 'r': c = '\r'; break;
            case 't': c = '\t'; break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case '"': c = '"';  break;
            case '\\': c = '\\'; break;
            case '/': c = '/';  break;
            case 'u': {
                /* 基本平面：转成 UTF-8 */
                if (p->i + 4 > p->n) { p->failed = 1; free(buf); return v_nil(); }
                char hex[5] = { p->s[p->i], p->s[p->i+1], p->s[p->i+2], p->s[p->i+3], 0 };
                unsigned cp = (unsigned)strtoul(hex, NULL, 16);
                p->i += 4;
                if (used + 4 > cap) { cap *= 2; buf = (char *)realloc(buf, cap); }
                if (cp < 0x80) buf[used++] = (char)cp;
                else if (cp < 0x800) {
                    buf[used++] = (char)(0xC0 | (cp >> 6));
                    buf[used++] = (char)(0x80 | (cp & 0x3F));
                } else {
                    buf[used++] = (char)(0xE0 | (cp >> 12));
                    buf[used++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                    buf[used++] = (char)(0x80 | (cp & 0x3F));
                }
                continue;
            }
            default: c = e;
            }
        }
        if (used + 2 > cap) { cap *= 2; buf = (char *)realloc(buf, cap); }
        buf[used++] = c;
    }
    if (p->i >= p->n) { p->failed = 1; free(buf); return v_nil(); }
    p->i++;   /* 结束引号 */
    Val r = v_text_n(buf, (uint32_t)used);
    free(buf);
    return r;
}

static Val jp_value(JParser *p) {
    jp_ws(p);
    if (p->i >= p->n) { p->failed = 1; return v_nil(); }
    char c = p->s[p->i];
    if (c == '"') return jp_string(p);
    if (c == '{') {
        p->i++;
        Obj *o = obj_new(T_MAP);
        jp_ws(p);
        if (p->i < p->n && p->s[p->i] == '}') { p->i++; return v_obj(o); }
        while (1) {
            jp_ws(p);
            Val k = jp_string(p);
            if (p->failed) { val_release(v_obj(o)); return v_nil(); }
            jp_ws(p);
            if (p->i >= p->n || p->s[p->i] != ':') { p->failed = 1; val_release(k); val_release(v_obj(o)); return v_nil(); }
            p->i++;
            Val v = jp_value(p);
            if (p->failed) { val_release(k); val_release(v); val_release(v_obj(o)); return v_nil(); }
            map_set(o, k.u.obj->text, v);
            val_release(k); val_release(v);
            jp_ws(p);
            if (p->i < p->n && p->s[p->i] == ',') { p->i++; continue; }
            if (p->i < p->n && p->s[p->i] == '}') { p->i++; break; }
            p->failed = 1; val_release(v_obj(o)); return v_nil();
        }
        return v_obj(o);
    }
    if (c == '[') {
        p->i++;
        Obj *o = seq_new();
        uint32_t cap = 8;
        o->items = (Val *)calloc(cap, sizeof(Val));
        jp_ws(p);
        if (p->i < p->n && p->s[p->i] == ']') { p->i++; return v_obj(o); }
        while (1) {
            Val v = jp_value(p);
            if (p->failed) { val_release(v_obj(o)); return v_nil(); }
            if (o->n == cap) { cap *= 2; o->items = (Val *)realloc(o->items, cap * sizeof(Val)); }
            o->items[o->n++] = v;
            jp_ws(p);
            if (p->i < p->n && p->s[p->i] == ',') { p->i++; continue; }
            if (p->i < p->n && p->s[p->i] == ']') { p->i++; break; }
            p->failed = 1; val_release(v_obj(o)); return v_nil();
        }
        return v_obj(o);
    }
    if (!strncmp(p->s + p->i, "true", 4))  { p->i += 4; return v_bool(1); }
    if (!strncmp(p->s + p->i, "false", 5)) { p->i += 5; return v_bool(0); }
    if (!strncmp(p->s + p->i, "null", 4))  { p->i += 4; return v_nil(); }
    {
        const char *start = p->s + p->i;
        char *end = NULL;
        double d = strtod(start, &end);
        if (end == start) { p->failed = 1; return v_nil(); }
        p->i += (size_t)(end - start);
        return v_num(d);
    }
}

static Val json_decode_text(const char *s) {
    JParser p = { s, 0, strlen(s), 0 };
    Val v = jp_value(&p);
    jp_ws(&p);
    if (p.failed || p.i != p.n) {
        val_release(v);
        runtime_error("类型", "JSON 解析失败");
        return v_nil();
    }
    return v;
}

static int json_is_valid(const char *s) {
    JParser p = { s, 0, strlen(s), 0 };
    Val v = jp_value(&p);
    jp_ws(&p);
    int ok = (!p.failed && p.i == p.n);
    val_release(v);
    return ok;
}

/* ============================================================
   标准库（原生实现）
   与 heng2/stdlib2.py 的 54 项一一对应，语义保持一致。
   纯函数；网络类由 CAP_CHECK 在指令层授权，这里只做实现。
   ============================================================ */

/* ---- 文本辅助 ---- */

static int text_eq(Val a, const char *s) {
    if (a.tag != T_TEXT || !a.u.obj) return 0;
    return a.u.obj->len == (uint32_t)strlen(s) && memcmp(a.u.obj->text, s, a.u.obj->len) == 0;
}

/* 取第 i 个 UTF-8 字符的字节起点；越界返回 len */
static uint32_t utf8_off(const char *s, uint32_t len, uint32_t i) {
    uint32_t p = 0, k = 0;
    while (p < len && k < i) {
        p++;
        while (p < len && ((unsigned char)s[p] & 0xC0) == 0x80) p++;
        k++;
    }
    return p;
}

/* 统计 UTF-8 字符数（不是字节数） */
static uint32_t utf8_len(const char *s, uint32_t len) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < len; i++)
        if (((unsigned char)s[i] & 0xC0) != 0x80) n++;
    return n;
}

static Val std_subtext(Val src, uint32_t start_ch, uint32_t count_ch) {
    uint32_t len = src.u.obj->len;
    const char *s = src.u.obj->text;
    uint32_t b0 = utf8_off(s, len, start_ch);
    uint32_t b1 = utf8_off(s, len, start_ch + count_ch);
    return v_text_n(s + b0, b1 - b0);
}

/* ---- 数学辅助 ---- */

static double num_at(Val *a, int i) { return a[i].u.num; }

/* round(x, d)：与 Python 的 round 对齐（银行家舍入在整数位差异极小，这里用常规四舍五入） */
static double round_digits(double x, int d) {
    if (d <= 0) return round(x);
    double f = pow(10.0, d);
    return round(x * f) / f;
}

/* ---- 序列辅助 ---- */

static Obj *seq_from(Val v) {
    Obj *o = seq_new();
    if (v.tag != T_SEQ || !v.u.obj) { o->cap = 0; return o; }
    uint32_t n = v.u.obj->n;
    o->items = (Val *)calloc(n ? n : 1, sizeof(Val));
    o->cap = n;
    o->n = 0;
    for (uint32_t i = 0; i < n; i++) {
        o->items[o->n++] = v.u.obj->items[i];
        val_retain(o->items[o->n - 1]);
    }
    return o;
}

/* 排序比较：数值按大小，文本按字典序；数值整体排在文本之前（与 Python 版一致） */
static int val_cmp(Val a, Val b) {
    int an = (a.tag == T_NUM), bn = (b.tag == T_NUM);
    if (an != bn) return an ? -1 : 1;
    if (an) return (a.u.num < b.u.num) ? -1 : (a.u.num > b.u.num) ? 1 : 0;
    const char *x = (a.tag == T_TEXT && a.u.obj) ? a.u.obj->text : "";
    const char *y = (b.tag == T_TEXT && b.u.obj) ? b.u.obj->text : "";
    return strcmp(x, y);
}

/* qsort 的比较器：val_cmp 是全序（数值优先、同类型内比较），
   满足严格弱序，可以直接喂给 qsort。 */
static int val_cmp_qsort(const void *a, const void *b) {
    return val_cmp(*(const Val *)a, *(const Val *)b);
}

static void sort_vals(Val *v, uint32_t n) {
    /* 曾是插入排序：4000 元素 12ms，16000 元素 197ms（O(n^2)）。
       换成 qsort 的 O(n log n)——实现只用一行，性能差一个数量级。 */
    if (n > 1) qsort(v, n, sizeof(Val), val_cmp_qsort);
}

/* ---- 时间 ---- */

static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* ============================================================
   标准库分发
   args 指向「第一个实参」，n 为实参数。
   ============================================================ */

static Val std_call(const char *name, Val *args, int n) {
    Val r = v_nil();
    /* 别名归一化：与 stdlib2.ALIAS 保持一致，避免同一函数两种写法行为不同 */
    static char norm[256];
    snprintf(norm, sizeof norm, "%s", name);
    struct { const char *from, *to; } al[] = {
        {"数值.", "数."}, {"文本.", "文字."}, {"列表.", "表."},
        {"数组.", "表."}, {"序列.", "表."}, {"映射.", "字典."},
    };
    for (size_t i = 0; i < sizeof al / sizeof al[0]; i++) {
        size_t fl = strlen(al[i].from);
        if (!strncmp(norm, al[i].from, fl)) {
            char tmp[256];
            snprintf(tmp, sizeof tmp, "%s%s", al[i].to, norm + fl);
            snprintf(norm, sizeof norm, "%s", tmp);
            break;
        }
    }
    name = norm;

    /* ---------------- 文字 ---------------- */
    if (strcmp(name, "文字.长度") == 0) {
        r = v_num(args[0].tag == T_TEXT ? (double)utf8_len(args[0].u.obj->text, args[0].u.obj->len) : 0);
    } else if (strcmp(name, "文字.字节数") == 0) {
        r = v_num(args[0].tag == T_TEXT ? (double)args[0].u.obj->len : 0);
    } else if (strcmp(name, "文字.大写") == 0 || strcmp(name, "文字.小写") == 0) {
        int upper = (strstr(name, "大写") != NULL);
        Obj *o = obj_new(T_TEXT);
        uint32_t ln = (args[0].tag == T_TEXT) ? args[0].u.obj->len : 0;
        o->text = (char *)malloc(ln + 1);
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        for (uint32_t i = 0; i < ln; i++) {
            char c = s[i];
            if (upper && c >= 'a' && c <= 'z') c = (char)(c - 32);
            else if (!upper && c >= 'A' && c <= 'Z') c = (char)(c + 32);
            o->text[i] = c;
        }
        o->text[ln] = 0; o->len = ln;
        r = v_obj(o);
    } else if (strcmp(name, "文字.去空白") == 0) {
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        uint32_t ln = (args[0].tag == T_TEXT) ? args[0].u.obj->len : 0;
        uint32_t b = 0, e = ln;
        while (b < e && (s[b] == ' ' || s[b] == '\t' || s[b] == '\r' || s[b] == '\n')) b++;
        while (e > b && (s[e-1] == ' ' || s[e-1] == '\t' || s[e-1] == '\r' || s[e-1] == '\n')) e--;
        r = v_text_n(s + b, e - b);
    } else if (strcmp(name, "文字.字符码") == 0) {
        /* 返回首字符的 Unicode 码点（UTF-8 解码）。
           中文处理与散列都依赖它：没有它就区分不了不同汉字。 */
        double cp = 0;
        if (args[0].tag == T_TEXT && args[0].u.obj->len > 0) {
            const unsigned char *p = (const unsigned char *)args[0].u.obj->text;
            if (p[0] < 0x80) cp = p[0];
            else if ((p[0] & 0xE0) == 0xC0) cp = ((p[0] & 0x1F) << 6) | (p[1] & 0x3F);
            else if ((p[0] & 0xF0) == 0xE0)
                cp = ((p[0] & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F);
            else if ((p[0] & 0xF8) == 0xF0)
                cp = ((p[0] & 0x07) << 18) | ((p[1] & 0x3F) << 12)
                   | ((p[2] & 0x3F) << 6) | (p[3] & 0x3F);
        }
        r = v_num(cp);
    } else if (strcmp(name, "文字.由码点") == 0) {
        unsigned cp = (unsigned)args[0].u.num;
        char buf[8]; int n2 = 0;
        if (cp < 0x80) buf[n2++] = (char)cp;
        else if (cp < 0x800) {
            buf[n2++] = (char)(0xC0 | (cp >> 6));
            buf[n2++] = (char)(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            buf[n2++] = (char)(0xE0 | (cp >> 12));
            buf[n2++] = (char)(0x80 | ((cp >> 6) & 0x3F));
            buf[n2++] = (char)(0x80 | (cp & 0x3F));
        } else {
            buf[n2++] = (char)(0xF0 | (cp >> 18));
            buf[n2++] = (char)(0x80 | ((cp >> 12) & 0x3F));
            buf[n2++] = (char)(0x80 | ((cp >> 6) & 0x3F));
            buf[n2++] = (char)(0x80 | (cp & 0x3F));
        }
        r = v_text_n(buf, (uint32_t)n2);
    } else if (strcmp(name, "文字.含") == 0) {
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        const char *sub = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        r = v_bool(strstr(s, sub) != NULL);
    } else if (strcmp(name, "文字.开头是") == 0) {
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        const char *p = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        r = v_bool(strncmp(s, p, strlen(p)) == 0);
    } else if (strcmp(name, "文字.结尾是") == 0) {
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        const char *p = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        size_t ls = strlen(s), lp = strlen(p);
        r = v_bool(lp <= ls && strcmp(s + ls - lp, p) == 0);
    } else if (strcmp(name, "文字.截") == 0) {
        if (args[0].tag != T_TEXT) { r = v_text(""); }
        else {
            int cnt = (int)args[1].u.num;
            r = std_subtext(args[0], 0, cnt < 0 ? 0 : (uint32_t)cnt);
        }
    } else if (strcmp(name, "文字.从") == 0) {
        /* 文字.从(s, n)：从第 n 个字符开始取到末尾。
           为什么需要它：文字.截(s, n) 的语义是「取前 n 个字符」，
           而几乎所有代码都误以为它是「从 n 开始」——
           IDE 里 `文字.截(事件, 3)` 想拿 "按钮|NEW" 里的 "NEW"，
           实际拿到 "按钮|"，于是所有按钮动作都匹配失败，
           表现就是「点了没反应」。这里提供一个语义明确的函数。 */
        if (args[0].tag == T_TEXT && args[0].u.obj) {
            const char *str = args[0].u.obj->text;
            int from = (int)args[1].u.num;
            uint32_t total = utf8_len(str, args[0].u.obj->len);
            if (from < 0) from = 0;
            if ((uint32_t)from >= total) {
                r = v_text("");
            } else {
                r = std_subtext(args[0], (uint32_t)from, total - (uint32_t)from);
            }
        } else {
            r = v_text("");
        }
    } else if (strcmp(name, "文字.切分") == 0) {
        Obj *o = seq_new();
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        const char *sep = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        uint32_t cap = 8;
        o->items = (Val *)calloc(cap, sizeof(Val));
        if (strlen(sep) == 0) {
            /* 空分隔符 = 按 UTF-8 字符切分（逐字），这是最符合直觉的读法，
               也是中文处理里最常用的需求。 */
            uint32_t ln = (args[0].tag == T_TEXT) ? args[0].u.obj->len : 0;
            for (uint32_t p = 0; p < ln; ) {
                uint32_t q = p + 1;
                while (q < ln && ((unsigned char)s[q] & 0xC0) == 0x80) q++;
                if (o->n == cap) { cap *= 2; o->items = (Val *)realloc(o->items, cap * sizeof(Val)); }
                o->items[o->n++] = v_text_n(s + p, q - p);
                p = q;
            }
        } else {
            size_t sl = strlen(sep);
            const char *p = s;
            while (1) {
                const char *q = strstr(p, sep);
                size_t seg = q ? (size_t)(q - p) : strlen(p);
                if (o->n == cap) { cap *= 2; o->items = (Val *)realloc(o->items, cap * sizeof(Val)); }
                o->items[o->n++] = v_text_n(p, (uint32_t)seg);
                if (!q) break;
                p = q + sl;
            }
        }
        r = v_obj(o);
    } else if (strcmp(name, "文字.合并") == 0) {
        /* 序列 + 分隔符 → 拼接。
           注意：早期对每个元素用 char[512] 中转，元素稍长就被静默截断，
           而截断点常常落在中文字符中间，写出的文件不是合法 UTF-8。
           报表场景里一行就有几十个汉字，非常容易踩到。
           现在文本元素直接取自身字节，非文本才用固定缓冲。 */
        const char *sep = (args[1].tag == T_TEXT && args[1].u.obj) ? args[1].u.obj->text : "";
        size_t sl = strlen(sep);
        size_t total = 1;
        uint32_t cnt = (args[0].tag == T_SEQ && args[0].u.obj) ? args[0].u.obj->n : 0;
        for (uint32_t i = 0; i < cnt; i++) {
            Val it = args[0].u.obj->items[i];
            size_t n = (it.tag == T_TEXT && it.u.obj) ? it.u.obj->len
                                                      : (size_t)snprintf(NULL, 0, "%.0f", it.u.num);
            total += n + (i ? sl : 0);
        }
        char *buf = (char *)malloc(total + 1);
        size_t used = 0;
        for (uint32_t i = 0; i < cnt; i++) {
            if (i && sl) { memcpy(buf + used, sep, sl); used += sl; }
            Val it = args[0].u.obj->items[i];
            if (it.tag == T_TEXT && it.u.obj) {
                memcpy(buf + used, it.u.obj->text, it.u.obj->len);
                used += it.u.obj->len;
            } else {
                char tmp[64];
                int wrote = snprintf(tmp, sizeof tmp, "%.0f", it.u.num);
                if (wrote > 0 && (size_t)wrote < sizeof tmp) {
                    memcpy(buf + used, tmp, (size_t)wrote);
                    used += (size_t)wrote;
                }
            }
        }
        buf[used] = 0;
        r = v_text_n(buf, (uint32_t)used);
        free(buf);
    } else if (strcmp(name, "文字.替换") == 0) {
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        const char *a = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        const char *b = (args[2].tag == T_TEXT) ? args[2].u.obj->text : "";
        size_t la = strlen(a);
        if (la == 0) { r = v_text(s); }
        else {
            size_t cap = strlen(s) + 64, used = 0;
            char *buf = (char *)malloc(cap);
            const char *p = s;
            while (1) {
                const char *q = strstr(p, a);
                size_t seg = q ? (size_t)(q - p) : strlen(p);
                size_t need = used + seg + strlen(b) + 1;
                if (need > cap) { cap = need * 2; buf = (char *)realloc(buf, cap); }
                memcpy(buf + used, p, seg); used += seg;
                if (!q) break;
                if (used + strlen(b) + 1 > cap) { cap = (used + strlen(b) + 1) * 2; buf = (char *)realloc(buf, cap); }
                memcpy(buf + used, b, strlen(b)); used += strlen(b);
                p = q + la;
            }
            buf[used] = 0;
            r = v_text_n(buf, (uint32_t)used);
            free(buf);
        }
    } else if (strcmp(name, "文字.分行") == 0) {
        Obj *o = seq_new();
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        uint32_t cap = 8;
        o->items = (Val *)calloc(cap, sizeof(Val));
        const char *start = s;
        for (const char *p = s; ; p++) {
            if (*p == '\n' || *p == 0) {
                uint32_t ln = (uint32_t)(p - start);
                /* drop trailing \r */
                if (ln > 0 && start[ln - 1] == '\r') ln--;
                if (!(*p == 0 && ln == 0 && o->n > 0)) {
                    if (o->n == cap) { cap *= 2; o->items = (Val *)realloc(o->items, cap * sizeof(Val)); }
                    o->items[o->n++] = v_text_n(start, ln);
                }
                if (*p == 0) break;
                start = p + 1;
            }
        }
        r = v_obj(o);
    }

    /* ---------------- 数学 ---------------- */
    else if (strcmp(name, "数.绝对值") == 0) r = v_num(fabs(num_at(args, 0)));
    else if (strcmp(name, "数.取整") == 0)     r = v_num(floor(num_at(args, 0)));
    else if (strcmp(name, "数.四舍五入") == 0) r = v_num(round_digits(num_at(args, 0), n > 1 ? (int)args[1].u.num : 0));
    else if (strcmp(name, "数.开方") == 0) {
        double x = num_at(args, 0);
        if (x < 0) r = v_num(NAN); else r = v_num(sqrt(x));
    }
    else if (strcmp(name, "数.乘方") == 0) r = v_num(pow(num_at(args, 0), num_at(args, 1)));
    else if (strcmp(name, "数.对数") == 0) r = v_num(log(num_at(args, 0)));
    else if (strcmp(name, "数.指数") == 0) r = v_num(exp(num_at(args, 0)));
    else if (strcmp(name, "数.正弦") == 0) r = v_num(sin(num_at(args, 0)));
    else if (strcmp(name, "数.余弦") == 0) r = v_num(cos(num_at(args, 0)));
    else if (strcmp(name, "数.最大") == 0) r = v_num(fmax(num_at(args, 0), num_at(args, 1)));
    else if (strcmp(name, "数.最小") == 0) r = v_num(fmin(num_at(args, 0), num_at(args, 1)));
    else if (strcmp(name, "数.圆周率") == 0) r = v_num(3.14159265358979323846);
    else if (strcmp(name, "数.整除") == 0) {
        double b = num_at(args, 1);
        if (b == 0) runtime_error("除零", "整除的除数不能为零");
        else r = v_num(floor(num_at(args, 0) / b));
    }
    else if (strcmp(name, "数.取余") == 0) {
        double b = num_at(args, 1);
        if (b == 0) runtime_error("除零", "取余的除数不能为零");
        else r = v_num(fmod(num_at(args, 0), b));
    }

    /* ---------------- 序列 ---------------- */
    else if (strcmp(name, "表.长度") == 0) {
        r = v_num(args[0].tag == T_SEQ ? (double)args[0].u.obj->n : 0);
    } else if (strcmp(name, "表.取") == 0) {
        if (args[0].tag != T_SEQ) { r = v_nil(); }
        else {
            int i = (int)args[1].u.num;
            uint32_t nn = args[0].u.obj->n;
            if (i < 0) i += (int)nn;
            if (i < 0 || i >= (int)nn) runtime_error("越界", "表.取 下标超出范围");
            else { r = args[0].u.obj->items[i]; val_retain(r); }
        }
    } else if (strcmp(name, "表.追加") == 0) {
        /* 写时复制：引用计数为 1 说明这个序列只有当前持有者，
           可以直接原地追加，避免每轮 O(n) 的整表复制。
           这曾让 循环 中反复 `xs = 表.追加(xs, v)` 退化为 O(n^2)——
           20 万次追加要 95 秒，是小函数引发的大问题。 */
        if (getenv("HENG_DEBUG_APPEND"))
            fprintf(stderr, "[append] rc=%u n=%u\n",
                    args[0].tag==T_SEQ && args[0].u.obj ? args[0].u.obj->rc : 0,
                    args[0].tag==T_SEQ && args[0].u.obj ? args[0].u.obj->n : 0);
        if (args[0].tag == T_SEQ && args[0].u.obj && args[0].u.obj->rc == 1) {
            Obj *o = args[0].u.obj;
            if (o->n >= o->cap) {
                o->cap = o->cap ? o->cap * 2 : 8;
                o->items = (Val *)realloc(o->items, o->cap * sizeof(Val));
            }
            o->items[o->n] = args[1]; val_retain(o->items[o->n]); o->n++;
            val_retain(args[0]);              /* 返回值共享同一对象 */
            r = args[0];
        } else {
            Obj *o = seq_from(args[0]);
            if (o->n >= o->cap) { o->cap = o->cap ? o->cap * 2 : 8; o->items = (Val *)realloc(o->items, o->cap * sizeof(Val)); }
            o->items[o->n] = args[1]; val_retain(o->items[o->n]); o->n++;
            r = v_obj(o);
        }
    } else if (strcmp(name, "表.连接") == 0) {
        Obj *o = seq_from(args[0]);
        if (args[1].tag == T_SEQ) {
            for (uint32_t i = 0; i < args[1].u.obj->n; i++) {
                if (o->n >= o->cap) {
                    o->cap = o->cap ? o->cap * 2 : 8;
                    o->items = (Val *)realloc(o->items, o->cap * sizeof(Val));
                }
                o->items[o->n] = args[1].u.obj->items[i];
                val_retain(o->items[o->n]); o->n++;
            }
        }
        r = v_obj(o);
    } else if (strcmp(name, "表.片段") == 0) {
        Obj *o = seq_new();
        if (args[0].tag == T_SEQ) {
            int b = (int)args[1].u.num, cnt = (int)args[2].u.num;
            int nn = (int)args[0].u.obj->n;
            if (b < 0) b = 0;
            if (cnt < 0) cnt = 0;
            if (b > nn) b = nn;
            if (b + cnt > nn) cnt = nn - b;
            o->items = (Val *)calloc(cnt ? cnt : 1, sizeof(Val));
            for (int i = 0; i < cnt; i++) {
                o->items[o->n] = args[0].u.obj->items[b + i];
                val_retain(o->items[o->n]); o->n++;
            }
        }
        r = v_obj(o);
    } else if (strcmp(name, "表.倒序") == 0) {
        Obj *o = seq_new();
        if (args[0].tag == T_SEQ) {
            uint32_t nn = args[0].u.obj->n;
            o->items = (Val *)calloc(nn ? nn : 1, sizeof(Val));
            for (uint32_t i = 0; i < nn; i++) {
                o->items[o->n] = args[0].u.obj->items[nn - 1 - i];
                val_retain(o->items[o->n]); o->n++;
            }
        }
        r = v_obj(o);
    } else if (strcmp(name, "表.排序") == 0) {
        Obj *o = seq_from(args[0]);
        sort_vals(o->items, o->n);
        r = v_obj(o);
    } else if (strcmp(name, "表.求和") == 0) {
        double t = 0;
        if (args[0].tag == T_SEQ)
            for (uint32_t i = 0; i < args[0].u.obj->n; i++) {
                Val v = args[0].u.obj->items[i];
                if (v.tag == T_NUM) t += v.u.num;
            }
        r = v_num(t);
    } else if (strcmp(name, "表.平均") == 0) {
        double t = 0; uint32_t c = 0;
        if (args[0].tag == T_SEQ) {
            c = args[0].u.obj->n;
            for (uint32_t i = 0; i < c; i++) if (args[0].u.obj->items[i].tag == T_NUM) t += args[0].u.obj->items[i].u.num;
        }
        r = v_num(c ? t / c : 0.0);
    } else if (strcmp(name, "表.最大") == 0 || strcmp(name, "表.最小") == 0) {
        int want_max = (strstr(name, "最大") != NULL);
        int found = 0; double best = 0;
        if (args[0].tag == T_SEQ)
            for (uint32_t i = 0; i < args[0].u.obj->n; i++) {
                Val v = args[0].u.obj->items[i];
                if (v.tag != T_NUM) continue;
                if (!found || (want_max ? v.u.num > best : v.u.num < best)) { best = v.u.num; found = 1; }
            }
        r = v_num(best);
    } else if (strcmp(name, "表.含") == 0) {
        int found = 0;
        if (args[0].tag == T_SEQ)
            for (uint32_t i = 0; i < args[0].u.obj->n; i++)
                if (val_cmp(args[0].u.obj->items[i], args[1]) == 0) { found = 1; break; }
        r = v_bool(found);
    } else if (strcmp(name, "表.去重") == 0) {
        /* 曾用嵌套循环比较，N 个元素 O(n^2)（8 千次 68ms）。
           改为「元素转文本 → 哈希集」，整体 O(n)。 */
        Obj *o = seq_new();
        if (args[0].tag == T_SEQ) {
            uint32_t nn = args[0].u.obj->n;
            o->items = (Val *)calloc(nn ? nn : 1, sizeof(Val));
            Obj *seen = (Obj *)calloc(1, sizeof(Obj));
            seen->kind = T_MAP;
            for (uint32_t i = 0; i < nn; i++) {
                char buf[2048];
                to_text(args[0].u.obj->items[i], buf, sizeof buf);
                if (map_find(seen, buf) >= 0) continue;
                map_set(seen, buf, v_bool(1));
                o->items[o->n] = args[0].u.obj->items[i];
                val_retain(o->items[o->n]); o->n++;
            }
            for (uint32_t i = 0; i < seen->n; i++) { free(seen->keys[i]); val_release(seen->vals[i]); }
            free(seen->keys); free(seen->vals); free(seen->buckets); free(seen->next);
            free(seen);
        }
        r = v_obj(o);
    } else if (strcmp(name, "表.过滤非空") == 0) {
        Obj *o = seq_new();
        if (args[0].tag == T_SEQ) {
            uint32_t nn = args[0].u.obj->n;
            o->items = (Val *)calloc(nn ? nn : 1, sizeof(Val));
            for (uint32_t i = 0; i < nn; i++) {
                char b[1024]; to_text(args[0].u.obj->items[i], b, sizeof b);
                const char *p = b;
                while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
                if (*p) {
                    o->items[o->n] = args[0].u.obj->items[i];
                    val_retain(o->items[o->n]); o->n++;
                }
            }
        }
        r = v_obj(o);
    }

    /* ---------------- 映射 ---------------- */
    else if (strcmp(name, "字典.取值") == 0) {
        const char *k = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        if (args[0].tag == T_MAP || args[0].tag == T_REC) {
            Val got = map_get(args[0].u.obj, k);
            r = (got.tag == T_NIL && n > 2) ? args[2] : got;
            val_retain(r);
        } else r = (n > 2) ? args[2] : v_nil();
    } else if (strcmp(name, "字典.设值") == 0) {
        Obj *o = obj_new(T_MAP);
        if (args[0].tag == T_MAP || args[0].tag == T_REC)
            for (uint32_t i = 0; i < args[0].u.obj->n; i++) {
                map_set(o, args[0].u.obj->keys[i], args[0].u.obj->vals[i]);
            }
        const char *k = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        map_set(o, k, args[2]);
        r = v_obj(o);
    } else if (strcmp(name, "字典.有键") == 0) {
        const char *k = (args[1].tag == T_TEXT) ? args[1].u.obj->text : "";
        int found = 0;
        if (args[0].tag == T_MAP || args[0].tag == T_REC)
            for (uint32_t i = 0; i < args[0].u.obj->n; i++)
                if (strcmp(args[0].u.obj->keys[i], k) == 0) { found = 1; break; }
        r = v_bool(found);
    } else if (strcmp(name, "字典.键") == 0) {
        Obj *o = seq_new();
        if (args[0].tag == T_MAP || args[0].tag == T_REC) {
            uint32_t nn = args[0].u.obj->n;
            o->items = (Val *)calloc(nn ? nn : 1, sizeof(Val));
            for (uint32_t i = 0; i < nn; i++) o->items[o->n++] = v_text(args[0].u.obj->keys[i]);
        }
        r = v_obj(o);
    } else if (strcmp(name, "字典.删键") == 0) {
        /* 原地删除。
           早期实现是「复制出一个不含该键的新映射返回」，但不改原对象——
           于是 `字典.删键(d, "a")` 之后 d 里 a 还在，
           文件列表这类「关闭一个文件」的操作完全失效。
           名字是「删键」，语义就该是就地改，返回被删的键数。 */
        const char *k = (args[1].tag == T_TEXT && args[1].u.obj) ? args[1].u.obj->text : "";
        int removed = 0;
        if ((args[0].tag == T_MAP || args[0].tag == T_REC) && args[0].u.obj && k[0]) {
            Obj *m = args[0].u.obj;
            uint32_t w = 0;
            for (uint32_t i = 0; i < m->n; i++) {
                if (strcmp(m->keys[i], k) == 0) {
                    removed = 1;
                    continue;                 /* 跳过被删的键 */
                }
                if (w != i) {
                    m->keys[w] = m->keys[i];
                    m->vals[w] = m->vals[i];
                }
                w++;
            }
            m->n = w;
            /* 必须重建哈希索引：映射底层是 buckets + next 链，
               只压缩 keys/vals 数组而不重建链，
               会让「删掉一个键再插回同名键」落进失效的桶，
               表现为 字典.大小 变成 0、有键 返回假。 */
            if (m->n == 0) {
                map_rehash(m, 1);
            } else {
                map_rehash(m, m->n);
            }
        }
        /* 返回原映射本身：既能 `字典.删键(d, k)` 就地删，
           也能 `d = 字典.删键(d, k)` 这种旧写法（赋值回来仍是同一个映射）。 */
        if (args[0].tag == T_MAP || args[0].tag == T_REC) {
            r = args[0];
            val_retain(args[0]);
        } else {
            r = v_num((double)removed);
        }
    } else if (strcmp(name, "字典.大小") == 0) {
        r = v_num((args[0].tag == T_MAP || args[0].tag == T_REC) ? (double)args[0].u.obj->n : 0);
    }

    /* ---------------- JSON ---------------- */
    else if (strcmp(name, "JSON.编码") == 0) {
        char *buf = json_encode_val(args[0], n > 1 ? (int)args[1].u.num : 0);
        r = v_text(buf);
        free(buf);
    } else if (strcmp(name, "JSON.解码") == 0) {
        r = json_decode_text(args[0].tag == T_TEXT ? args[0].u.obj->text : "");
    } else if (strcmp(name, "JSON.有效") == 0) {
        r = v_bool(json_is_valid(args[0].tag == T_TEXT ? args[0].u.obj->text : ""));
    }

    /* ---------------- 时间 ---------------- */
    else if (strcmp(name, "时间.现在") == 0) r = v_num(now_seconds());
    else if (strcmp(name, "时间.毫秒") == 0) r = v_num(floor(now_seconds() * 1000.0));
    else if (strcmp(name, "时间.格式化") == 0) {
        time_t t = (time_t)args[0].u.num;
        struct tm tmv;
        localtime_r(&t, &tmv);
        const char *fmt = (n > 1 && args[1].tag == T_TEXT) ? args[1].u.obj->text : "%Y-%m-%d %H:%M:%S";
        char buf[256];
        strftime(buf, sizeof buf, fmt, &tmv);
        r = v_text(buf);
    }

    /* ---------------- 网络（能力已由 CAP_CHECK 授权） ---------------- */
    else if (strcmp(name, "网络.编码") == 0) {
        const char *s = (args[0].tag == T_TEXT) ? args[0].u.obj->text : "";
        size_t cap = strlen(s) * 3 + 1, used = 0;
        char *buf = (char *)malloc(cap);
        static const char *hex = "0123456789ABCDEF";
        for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
            if ((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
                (*p >= '0' && *p <= '9') || *p == '-' || *p == '_' || *p == '.' || *p == '~') {
                buf[used++] = (char)*p;
            } else {
                buf[used++] = '%';
                buf[used++] = hex[*p >> 4];
                buf[used++] = hex[*p & 15];
            }
        }
        buf[used] = 0;
        r = v_text(buf);
        free(buf);
    } else if (strcmp(name, "网络.取") == 0 || strcmp(name, "网络.发") == 0) {
        r = http_call(name, args, n);
    }

    else {
        char msg[256];
        snprintf(msg, sizeof msg, "未实现的标准库函数：%s", name);
        raise_err("未定义", msg);
    }
    return r;
}

/* 单任务执行：所有执行状态（pc/sp/帧栈）都来自 task，
   因此同一份代码可以被多个线程同时执行而互不干扰。 */
/* 从字节码里读一个 f64 操作数（小端，与 Python struct.pack("<d") 对齐）。
   注意：文件级读取已有 rd_f64()，这里另起名避免覆盖。 */
static double rd_op_f64(const uint8_t *code, int *pc) {
    double d;
    memcpy(&d, code + *pc, 8);
    *pc += 8;
    return d;
}

static void run_task(Task *task);

/* 同步调用一个衡语函数（供高阶函数回调使用）。
   实现方式：保存当前任务的执行上下文 → 用临时 Task 跑完 → 恢复。
   这样不必把 VM 改造成可重入，代价是一次上下文切换。
   注意：回调里若再嵌套高阶函数，靠保存/恢复保证正确。 */
static void run_task(Task *task);

/* 同步调用一个衡语函数（供高阶函数回调使用）。

   run_task 内部的执行状态全部通过宏映射到 task 自身字段
   （slots / stack / frame_top 等），所以新建一个临时 Task 天然隔离，
   不需要保存恢复任何全局状态——只要保证 g_err_pending 在调用前后一致。
*/
static Val call_fn_sync_ex(uint16_t fi, Val *args, int nargs, Task *caller,
                            Val *caps, int ncap);

static Val call_fn_sync(uint16_t fi, Val *args, int nargs) {
    return call_fn_sync_ex(fi, args, nargs, NULL, NULL, 0);
}

static Val call_fn_sync_ex(uint16_t fi, Val *args, int nargs, Task *caller,
                           Val *caps, int ncap) {
    Task *t = task_create(fi, args, (uint8_t)nargs, -1);
    if (!t) return v_nil();

    /* lambda 使用独立槽位区：参数 + 捕获值。捕获值放在帧尾，
       lambda 体内用 LOAD_FREE 读取（按值快照，不会悬垂）。 */
    (void)caller;
    if (ncap > 0 && caps) {
        uint32_t ns = (uint32_t)g_funcs[fi].n_slots;
        free(t->slots);
        t->slots = (Val *)calloc(ns + ncap, sizeof(Val));
        for (int i = 0; i < nargs; i++) t->slots[i] = val_clone(args[i]);
        for (int i = 0; i < ncap; i++) t->slots[ns + i] = val_clone(caps[i]);
        t->slot_top = ns + ncap;
    }

    int saved_err = g_err_pending;
    g_err_pending = 0;

    run_task(t);

    Val out = t->result;
    val_retain(out);

    g_err_pending = saved_err;

    if (!t->shared_slots) free(t->slots);
    free(t->stack);
    free(t->ffunc); free(t->fbase); free(t->fret);
    free(t->fsp); free(t->fcode); free(t->fcode_len);
    free(t->children);
    free(t);
    return out;
}

/* ============================================================
   调试器：断点 + 单步 + 变量查看
   ------------------------------------------------------------
   设计：不做「图形调试器 + 独立进程」那一套（太重），
   而是在 VM 主循环里插一个钩子，由环境变量驱动：

     HENG_BP=12,25,40     在这些「指令序号」处停下
     HENG_STEP=1          每步都停（单步）
     HENG_DBG_CMD=文件    调试命令队列：写到文件里，VM 每步读一行

   停下时把当前状态写进 HENG_DBG_LOG 指定的文件，
   编辑器读这个文件就知道「停在哪、变量是什么值」。

   为什么不直接用 stdin 交互：GUI 进程没有终端可读，读 stdin 会卡死。
   文件通道对 GUI 和命令行都适用。
   ============================================================ */
static int   g_dbg_on = 0;
static int   g_dbg_step = 0;
static int   g_bp[64];
static int   g_bp_n = 0;
static FILE *g_dbg_log = NULL;
static const char *g_dbg_cmdpath = NULL;
static int   g_dbg_paused = 0;      /* 1=已暂停，等命令 */
static int   g_dbg_quit = 0;

static void dbg_init(void) {
    const char *bp = getenv("HENG_BP");
    const char *st = getenv("HENG_STEP");
    const char *lg = getenv("HENG_DBG_LOG");
    const char *cp = getenv("HENG_DBG_CMD");
    if (!bp && !st) return;
    g_dbg_on = 1;
    g_dbg_step = (st && atoi(st) != 0) ? 1 : 0;
    g_dbg_cmdpath = cp;
    if (lg) {
        g_dbg_log = fopen(lg, "w");
        if (g_dbg_log) setvbuf(g_dbg_log, NULL, _IONBF, 0);
    }
    if (bp) {
        const char *p = bp;
        while (*p && g_bp_n < 64) {
            while (*p == ',' || *p == ' ') p++;
            if (!*p) break;
            g_bp[g_bp_n++] = atoi(p);
            while (*p && *p != ',') p++;
        }
    }
}

static void dbg_snap(const char *why, int pc, Val *L, int slot_top) {
    if (!g_dbg_log) return;
    fprintf(g_dbg_log, "STOP|%s|pc=%d|slots=%d\n", why, pc, slot_top);
    for (int i = 0; i < slot_top && i < 64; i++) {
        if (L[i].tag == T_NUM) {
            fprintf(g_dbg_log, "  [%d] 数 = %g\n", i, L[i].u.num);
        } else if (L[i].tag == T_TEXT && L[i].u.obj) {
            fprintf(g_dbg_log, "  [%d] 文本 = \"%s\"\n", i, L[i].u.obj->text);
        } else if (L[i].tag == T_BOOL) {
            fprintf(g_dbg_log, "  [%d] 真假 = %s\n", i, L[i].u.num != 0 ? "真" : "假");
        } else {
            fprintf(g_dbg_log, "  [%d] tag=%d\n", i, L[i].tag);
        }
    }
}

/* 返回 1 表示应该暂停（调用方据此等待命令） */
static int dbg_should_break(int pc) {
    if (!g_dbg_on) return 0;
    if (g_dbg_step) return 1;
    for (int i = 0; i < g_bp_n; i++) {
        if (g_bp[i] == pc) return 1;
    }
    return 0;
}

/* 暂停：把状态写盘，然后等命令文件出现关键词。
   支持的命令：c(继续) s(单步) q(退出)
   命令文件读完后会清空，避免重复响应。 */
static void dbg_wait(const char *why, int pc, Val *L, int slot_top) {
    dbg_snap(why, pc, L, slot_top);
    if (!g_dbg_log) return;
    fprintf(g_dbg_log, "WAITING\n");
    if (!g_dbg_cmdpath) {
        /* 没有命令通道：断点模式下默认停一次就继续，避免卡死 */
        fprintf(g_dbg_log, "RESUME(no-cmd)\n");
        return;
    }
    for (int spin = 0; spin < 60000; spin++) {         /* 最多等 60 秒 */
        FILE *f = fopen(g_dbg_cmdpath, "r");
        if (f) {
            char cmd[32] = "";
            if (fgets(cmd, sizeof cmd, f)) {
                fclose(f);
                FILE *clr = fopen(g_dbg_cmdpath, "w");
                if (clr) fclose(clr);
                if (cmd[0] == 'c') { g_dbg_step = 0; fprintf(g_dbg_log, "RESUME(c)\n"); return; }
                if (cmd[0] == 's') { g_dbg_step = 1; fprintf(g_dbg_log, "RESUME(s)\n"); return; }
                if (cmd[0] == 'q') { g_dbg_quit = 1; fprintf(g_dbg_log, "QUIT\n"); return; }
            } else {
                fclose(f);
            }
        }
        usleep(50000);
    }
    fprintf(g_dbg_log, "RESUME(timeout)\n");
}

static void run_task(Task *task) {
    if (g_trace) fprintf(stderr, "  [task start] slots[0].tag=%d slots[1].tag=%d base0=%d\n",
                         task->slots[0].tag, task->slots[1].tag, task->fbase[0]);
    const uint8_t *code = task->code;
    uint32_t code_len = task->code_len;
    int pc = task->pc;
    Val *S = task->stack;
    int sp = task->sp;
    Val *L = task->slots;
    int slot_top = task->slot_top;
    /* 帧栈指针化：让下面的宏式访问尽量少改代码 */
    #define g_frame_top  (task->frame_top)
    #define g_fbase      (task->fbase)
    #define g_ffunc      (task->ffunc)
    #define g_fret       (task->fret)
    #define g_fsp        (task->fsp)
    #define g_fcode      (task->fcode)
    #define g_fcode_len  (task->fcode_len)

    while (pc < (int)code_len) {
        g_cur_pc = pc;      /* 供报错定位 */
        /* 调试钩子：断点/单步。放在指令边界，语义干净。 */
        if (g_dbg_on && dbg_should_break(pc)) {
            dbg_wait("break", pc, L, slot_top);
            if (g_dbg_quit) return;
        }
        /* 异常分派点：任何指令置了错误标志，都在这里统一处理，
           与「指令边界才可能抛错」的语义完全一致。 */
        if (g_err_pending) {
            g_err_pending = 0;
            if (g_handler_n == 0) raise_err(g_err_cat, g_err_msg);
            Handler *h = &g_handlers[g_handler_n - 1];
            /* 回退到登记时的帧与栈深 */
            while (g_frame_top > h->frame_depth) {
                g_frame_top--;
                int base = g_fbase[g_frame_top];
                for (int i = base; i < slot_top; i++) val_release(L[i]);
                slot_top = base;
            }
            while (sp > h->stack_top) { sp--; val_release(S[sp]); }
            sp = h->stack_top;
            /* 恢复到「被中断帧自己」的代码：g_fcode[f] 记录的是帧 f 的
               调用者代码，所以当前帧 f 的代码要用 g_fcode[f] 的上一条关系推不出来——
               改为直接用 g_ffunc 反查（这是唯一可靠来源）。 */
            {
                int cur = (int)g_ffunc[g_frame_top - 1];
                code = (cur >= 0) ? g_funcs[cur].code : g_entry;
                code_len = (cur >= 0) ? g_funcs[cur].code_len : g_entry_len;
            }
            if (h->catch_pc >= 0) {
                g_handler_n--;      /* 进入 catch 即撤销，catch 内再抛错交给外层 */
                S[sp++] = v_text(g_err_msg);
                pc = h->catch_pc;
            } else if (h->finally_pc >= 0) {
                g_handler_n--;
                pc = h->finally_pc;
            } else {
                g_handler_n--;
                raise_err(g_err_cat, g_err_msg);
            }
            continue;
        }

        uint8_t op = code[pc++];
        switch (op) {
        case OP_PUSH_NUM:  S[sp++] = v_num(rd_d(code, &pc)); break;
        case OP_PUSH_NIL:  S[sp++] = v_nil(); break;
        case OP_PUSH_TRUE: S[sp++] = v_bool(1); break;
        case OP_PUSH_FALSE:S[sp++] = v_bool(0); break;
        case OP_PUSH_CONST: {
            uint32_t i = rd32(code, &pc);
            Const *c = &g_pool[i];
            if (c->kind == 0) S[sp++] = v_num(c->v.num);
            else if (c->kind == 2) S[sp++] = v_bool(c->v.b);
            else if (c->kind == 3) S[sp++] = v_nil();
            else S[sp++] = v_text(c->v.text);
            val_retain(S[sp-1]);
            break;
        }
        case OP_POP:  val_release(S[--sp]); break;
        case OP_DUP:  S[sp] = S[sp-1]; val_retain(S[sp]); sp++; break;
        case OP_SWAP: { Val t = S[sp-1]; S[sp-1] = S[sp-2]; S[sp-2] = t; break; }

        case OP_LOAD_LOCAL: {
            uint16_t s = rd16(code, &pc);
            int idx = g_fbase[g_frame_top-1] + s;
            S[sp] = L[idx];
            if (g_trace && task->group >= -1) fprintf(stderr, "    LOAD %u idx=%d tag=%d\n", s, idx, L[idx].tag);
            val_retain(S[sp]);
            sp++;
            break;
        }
        case OP_STORE_LOCAL: {
            uint16_t s = rd16(code, &pc);
            int idx = g_fbase[g_frame_top-1] + s;
            val_release(L[idx]);
            L[idx] = S[--sp];
            if (g_trace) fprintf(stderr, "    STORE %u idx=%d tag=%d\n", s, idx, L[idx].tag);
            break;
        }
        case OP_LOAD_GLOBAL: {
            uint16_t s = rd16(code, &pc);
            S[sp] = g_globals[s]; val_retain(S[sp]); sp++;
            break;
        }
        case OP_STORE_GLOBAL: {
            uint16_t s = rd16(code, &pc);
            g_globals[s] = S[--sp];
            break;
        }

        case OP_HIGHER_ORDER: {
            /* 栈：序列、回调函数（、折叠初值）。VM 内部循环调回调。 */
            uint8_t kind = code[pc++];
            uint8_t nargs = code[pc++];
            Val init = v_nil();
            if (nargs >= 3) init = S[--sp];
            Val fnv = S[--sp];
            Val seq = S[--sp];

            if (fnv.tag != T_FUNC || seq.tag != T_SEQ) {
                runtime_error("类型", "高阶函数需要（序列, 做法）");
                val_release(init);
                break;
            }
            if (!fnv.u.obj) { runtime_error("类型", "回调不是有效做法"); break; }
            uint16_t fi = (uint16_t)fnv.u.obj->len;
            Val *caps = fnv.u.obj->items;
            int ncap = (int)fnv.u.obj->n;
            Obj *o = seq_new();
            uint32_t cap = seq.u.obj->n ? seq.u.obj->n : 8;
            if (kind != 2) o->items = (Val *)calloc(cap, sizeof(Val));

            Val acc = init;   /* 折叠用 */
            if (kind == 2) val_retain(acc);

            for (uint32_t i2 = 0; i2 < seq.u.obj->n; i2++) {
                Val elem = seq.u.obj->items[i2];
                val_retain(elem);
                Val argv[2];
                int argc;
                if (kind == 2) { argv[0] = acc; argv[1] = elem; argc = 2; }
                else           { argv[0] = elem; argc = 1; }
                Val r = call_fn_sync_ex(fi, argv, argc, task, caps, ncap);

                if (kind == 0) {            /* 映射 */
                    o->items[o->n++] = r;
                } else if (kind == 1) {     /* 过滤 */
                    int keep = (r.tag == T_BOOL) ? r.u.b : (r.tag != T_NIL);
                    val_release(r);
                    if (keep) { o->items[o->n++] = elem; val_retain(elem); }
                    else val_release(elem);
                } else {                    /* 折叠 */
                    val_release(acc); val_release(elem);
                    acc = r; val_retain(acc);
                }
                if (kind != 1) val_release(elem);
            }

            val_release(seq);
            val_release(fnv);
            if (kind == 2) { S[sp++] = acc; val_release(init); }
            else { o->n = o->n; S[sp++] = v_obj(o); }
            break;
        }

        case OP_LOAD_FREE: {
            /* 捕获值位于当前帧的 n_slots 之后：帧基准 + 本函数 n_slots + i */
            uint16_t i = rd16(code, &pc);
            uint32_t fi = g_ffunc[g_frame_top-1];
            int idx = g_fbase[g_frame_top-1] + (int)g_funcs[fi].n_slots + i;
            Val v = L[idx];
            val_retain(v);
            S[sp++] = v;
            break;
        }

        case OP_MAKE_CLOSURE: {
            /* 闭包：Obj(kind=T_FUNC) 里存 函数索引 + 捕获值数组。
               len 存函数索引，items/n 存捕获值。 */
            uint16_t fi = rd16(code, &pc);
            uint8_t ncap = code[pc++];
            Obj *o = obj_new(T_FUNC);
            o->len = fi;
            o->n = ncap;
            o->items = ncap ? (Val *)calloc(ncap, sizeof(Val)) : NULL;
            /* 栈上依次是 cap0..capN-1，按序搬入并转移所有权 */
            for (int i = 0; i < ncap; i++) {
                o->items[i] = S[sp - ncap + i];
            }
            sp -= ncap;
            S[sp++] = v_obj(o);
            break;
        }

        case OP_MAKE_FUNC: {
            /* 统一表示：即便是无捕获的函数，也包成闭包对象（ncap = 0），
               这样 CALL_VALUE 只需处理一种形态。
               早期用「T_FUNC + u.num 存索引」，与闭包（u.obj）在 union 里冲突，
               曾导致把数值当地址解引用而段错误。 */
            uint16_t fi = rd16(code, &pc);
            Obj *o = obj_new(T_FUNC);
            o->len = fi;
            o->n = 0;
            o->items = NULL;
            S[sp++] = v_obj(o);
            break;
        }
        case OP_CALL_VALUE: {
            uint16_t s = rd16(code, &pc);
            uint8_t nargs = code[pc++];
            int src = g_fbase[g_frame_top-1] + s;
            if (L[src].tag != T_FUNC) {
                runtime_error("类型", "该值不是可调用的做法");
                break;
            }
            Obj *clo = L[src].u.obj;
            if (!clo) { runtime_error("类型", "函数值无效"); break; }
            uint16_t fi = (uint16_t)clo->len;
            uint8_t ncap = (uint8_t)clo->n;
            Val *caps = clo->items;

            Func *fn = &g_funcs[fi];
            int base = slot_top;
            int total = fn->n_slots + ncap;
            if (base + total >= MAX_SLOTS) { raise_err("越界", "局部变量空间不足"); break; }
            for (int i = 0; i < nargs; i++) L[base + i] = S[sp - nargs + i];
            for (int i = nargs; i < fn->n_slots; i++) L[base + i] = v_nil();
            /* 捕获值放在帧尾：lambda 体用 LOAD_FREE i 读第 i 个 */
            for (int i = 0; i < ncap; i++) {
                L[base + fn->n_slots + i] = caps[i];
                val_retain(caps[i]);
            }
            slot_top += total;
            sp -= nargs;
            g_ffunc[g_frame_top] = fi;
            g_fbase[g_frame_top] = base;
            g_fret[g_frame_top] = pc;
            g_fsp[g_frame_top] = sp;
            g_fcode[g_frame_top] = code;
            g_fcode_len[g_frame_top] = code_len;
            g_frame_top++;
            if (g_frame_top >= MAX_FRAMES) raise_err("越界", "调用层数过深");
            code = g_funcs[fi].code;
            code_len = g_funcs[fi].code_len;
            pc = 0;
            break;
        }

        /* ---- 算术与比较的槽位超指令 ---- */
        case OP_SUB_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            L[idx2].u.num -= imm2;            break;
        }
        case OP_MUL_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            L[idx2].u.num *= imm2;            break;
        }
        case OP_DIV_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            if (imm2 == 0) { runtime_error("除零", "除数不能为零"); break; } L[idx2].u.num /= imm2;            break;
        }
        case OP_MOD_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            if (imm2 == 0) { runtime_error("除零", "取模的除数不能为零"); break; } L[idx2].u.num = fmod(L[idx2].u.num, imm2);            break;
        }
        case OP_SUB_SLOT_SLOT: {
            uint16_t d2 = rd16(code, &pc);
            uint16_t s2 = rd16(code, &pc);
            int base2 = g_fbase[g_frame_top-1];
            L[base2 + d2].u.num -= L[base2 + s2].u.num;            break;
        }
        case OP_MUL_SLOT_SLOT: {
            uint16_t d2 = rd16(code, &pc);
            uint16_t s2 = rd16(code, &pc);
            int base2 = g_fbase[g_frame_top-1];
            L[base2 + d2].u.num *= L[base2 + s2].u.num;            break;
        }
        case OP_GT_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            S[sp++] = v_bool(L[idx2].u.num >  imm2);            break;
        }
        case OP_LE_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            S[sp++] = v_bool(L[idx2].u.num <= imm2);            break;
        }
        case OP_GE_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            S[sp++] = v_bool(L[idx2].u.num >= imm2);            break;
        }
        case OP_EQ_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            S[sp++] = v_bool(L[idx2].u.num == imm2);            break;
        }
        case OP_NE_SLOT_IMM: {
            uint16_t s2 = rd16(code, &pc);
            double imm2 = rd_op_f64(code, &pc);
            int idx2 = g_fbase[g_frame_top-1] + s2;
            S[sp++] = v_bool(L[idx2].u.num != imm2);            break;
        }

        case OP_SLOT_APPEND_STACK: {
            uint16_t ds = rd16(code, &pc);
            Val v = S[--sp];
            int base = g_fbase[g_frame_top-1];
            Val cur = L[base + ds];
            if (cur.tag != T_SEQ || !cur.u.obj) {
                val_release(v);
                runtime_error("类型", "追加的目标必须是序列");
                break;
            }
            if (cur.u.obj->rc != 1) {
                /* 有别名：复制后再追加，保持值语义 */
                val_retain(cur);
                Obj *cp = seq_from(cur);
                if (cp->n >= cp->cap) {
                    cp->cap = cp->cap ? cp->cap * 2 : 8;
                    cp->items = (Val *)realloc(cp->items, cp->cap * sizeof(Val));
                }
                cp->items[cp->n++] = v;
                val_release(L[base + ds]);
                L[base + ds] = v_obj(cp);
                val_release(cur);
                break;
            }
            Obj *o = cur.u.obj;
            if (o->n >= o->cap) {
                o->cap = o->cap ? o->cap * 2 : 8;
                o->items = (Val *)realloc(o->items, o->cap * sizeof(Val));
            }
            o->items[o->n++] = v;
            break;
        }

        case OP_SLOT_CONCAT_SLOT: {
            uint16_t ds = rd16(code, &pc);
            uint16_t ss = rd16(code, &pc);
            int base = g_fbase[g_frame_top-1];
            Val cur = L[base + ds];
            Val src = L[base + ss];
            if (cur.tag != T_SEQ || !cur.u.obj) { runtime_error("类型", "连接的目标必须是序列"); break; }
            if (src.tag != T_SEQ || !src.u.obj) break;
            /* 有别名时先复制，保持值语义（与 表.追加 一致） */
            Obj *o;
            if (cur.u.obj->rc != 1) {
                val_retain(cur);
                o = seq_from(cur);
                val_release(L[base + ds]);
                L[base + ds] = v_obj(o);
                val_release(cur);
                val_retain(L[base + ds]);
            } else {
                o = cur.u.obj;
            }
            for (uint32_t i = 0; i < src.u.obj->n; i++) {
                if (o->n >= o->cap) {
                    o->cap = o->cap ? o->cap * 2 : 8;
                    o->items = (Val *)realloc(o->items, o->cap * sizeof(Val));
                }
                o->items[o->n] = src.u.obj->items[i];
                val_retain(o->items[o->n]);
                o->n++;
            }
            break;
        }

        case OP_SLOT_APPEND_SLOT: {
            uint16_t ds = rd16(code, &pc);
            uint16_t vs = rd16(code, &pc);
            int base = g_fbase[g_frame_top-1];
            Val cur = L[base + ds];
            if (cur.tag != T_SEQ || !cur.u.obj) { runtime_error("类型", "追加的目标必须是序列"); break; }
            if (cur.u.obj->rc != 1) {
                /* 有别名（如 b = a）：必须复制后再追加，保持值语义 */
                Val src = cur; val_retain(src);
                Obj *cp = seq_from(src);
                if (cp->n >= cp->cap) {
                    cp->cap = cp->cap ? cp->cap * 2 : 8;
                    cp->items = (Val *)realloc(cp->items, cp->cap * sizeof(Val));
                }
                cp->items[cp->n] = L[base + vs];
                val_retain(cp->items[cp->n]);
                cp->n++;
                val_release(L[base + ds]);
                L[base + ds] = v_obj(cp);
                val_release(src);
                break;
            }
            Obj *o = cur.u.obj;
            if (o->n >= o->cap) {
                o->cap = o->cap ? o->cap * 2 : 8;
                o->items = (Val *)realloc(o->items, o->cap * sizeof(Val));
            }
            o->items[o->n] = L[base + vs];
            val_retain(o->items[o->n]);
            o->n++;
            break;
        }
        case OP_SLOT_APPEND_IMM: {
            uint16_t ds = rd16(code, &pc);
            double imm = rd_op_f64(code, &pc);
            int base = g_fbase[g_frame_top-1];
            Val cur = L[base + ds];
            if (cur.tag != T_SEQ || !cur.u.obj) { runtime_error("类型", "追加的目标必须是序列"); break; }
            if (cur.u.obj->rc != 1) {
                Val src = cur; val_retain(src);
                Obj *cp = seq_from(src);
                if (cp->n >= cp->cap) {
                    cp->cap = cp->cap ? cp->cap * 2 : 8;
                    cp->items = (Val *)realloc(cp->items, cp->cap * sizeof(Val));
                }
                cp->items[cp->n] = v_num(imm);
                cp->n++;
                val_release(L[base + ds]);
                L[base + ds] = v_obj(cp);
                val_release(src);
                break;
            }
            Obj *o = cur.u.obj;
            if (o->n >= o->cap) {
                o->cap = o->cap ? o->cap * 2 : 8;
                o->items = (Val *)realloc(o->items, o->cap * sizeof(Val));
            }
            o->items[o->n] = v_num(imm);
            o->n++;
            break;
        }

        /* ---- 索引访问超指令：直接读槽取元素，跳过两次间接访存 ---- */
        case OP_SLOT_INDEX_SLOT: {
            uint16_t cs = rd16(code, &pc);
            uint16_t is = rd16(code, &pc);
            int base = g_fbase[g_frame_top-1];
            Val c = L[base + cs];
            if (c.tag != T_SEQ) { runtime_error("类型", "该值不支持下标访问"); break; }
            int n2 = (int)L[base + is].u.num;
            if (n2 < 0) n2 += (int)c.u.obj->n;
            if (n2 < 0 || n2 >= (int)c.u.obj->n) { runtime_error("越界", "下标超出范围"); break; }
            Val out = c.u.obj->items[n2];
            val_retain(out);
            S[sp++] = out;
            break;
        }
        case OP_SLOT_INDEX_IMM: {
            uint16_t cs = rd16(code, &pc);
            double di = rd_op_f64(code, &pc);
            int base = g_fbase[g_frame_top-1];
            Val c = L[base + cs];
            if (c.tag != T_SEQ) { runtime_error("类型", "该值不支持下标访问"); break; }
            int n2 = (int)di;
            if (n2 < 0) n2 += (int)c.u.obj->n;
            if (n2 < 0 || n2 >= (int)c.u.obj->n) { runtime_error("越界", "下标超出范围"); break; }
            Val out = c.u.obj->items[n2];
            val_retain(out);
            S[sp++] = out;
            break;
        }
        case OP_SLOT_INDEX_STORE: {
            uint16_t cs = rd16(code, &pc);
            uint16_t is = rd16(code, &pc);
            uint16_t vs = rd16(code, &pc);
            int base = g_fbase[g_frame_top-1];
            Val c = L[base + cs];
            if (c.tag != T_SEQ) { runtime_error("类型", "该值不支持下标赋值"); break; }
            /* 写时复制：多处引用时先复制，保证 `b = a; b[i] = v` 不动到 a */
            if (c.u.obj->rc > 1) {
                val_retain(c);
                Obj *cp = seq_from(c);
                val_release(L[base + cs]);
                L[base + cs] = v_obj(cp);
                val_release(c);
                c = L[base + cs];
            }
            int n2 = (int)L[base + is].u.num;
            if (n2 < 0) n2 += (int)c.u.obj->n;
            if (n2 < 0 || n2 >= (int)c.u.obj->n) { runtime_error("越界", "下标赋值超出范围"); break; }
            val_release(c.u.obj->items[n2]);
            c.u.obj->items[n2] = L[base + vs];
            val_retain(L[base + vs]);
            break;
        }

        /* ---- 超指令：取槽→算→写回，省掉 LOAD/STORE 的栈往返 ---- */
        case OP_ADD_SLOT_IMM: {
            uint16_t s = rd16(code, &pc);
            double imm = rd_op_f64(code, &pc);
            int idx = g_fbase[g_frame_top-1] + s;
            L[idx].u.num += imm;
            break;
        }
        case OP_ADD_SLOT_SLOT: {
            uint16_t dst = rd16(code, &pc);
            uint16_t src = rd16(code, &pc);
            int base = g_fbase[g_frame_top-1];
            L[base + dst].u.num += L[base + src].u.num;
            break;
        }
        case OP_LT_SLOT_IMM: {
            uint16_t s = rd16(code, &pc);
            double imm = rd_op_f64(code, &pc);
            int idx = g_fbase[g_frame_top-1] + s;
            Val r = v_bool(L[idx].u.num < imm);
            S[sp++] = r;
            break;
        }

        /* ---- 数值特化：直取 double，不做 tag 判断 ---- */
        case OP_ADD_NUM: { double y = S[--sp].u.num; S[sp-1].u.num += y; break; }
        case OP_SUB_NUM: { double y = S[--sp].u.num; S[sp-1].u.num -= y; break; }
        case OP_MUL_NUM: { double y = S[--sp].u.num; S[sp-1].u.num *= y; break; }
        case OP_DIV_NUM: {
            double y = S[--sp].u.num;
            if (y == 0) { runtime_error("除零", "除数不能为零"); break; }
            S[sp-1].u.num /= y;
            break;
        }
        case OP_MOD_NUM: {
            double y = S[--sp].u.num;
            if (y == 0) { runtime_error("除零", "取模的除数不能为零"); break; }
            S[sp-1].u.num = fmod(S[sp-1].u.num, y);
            break;
        }
        case OP_LT_NUM: { double y = S[--sp].u.num; Val r = v_bool(S[sp-1].u.num <  y); sp--; S[sp++] = r; break; }
        case OP_LE_NUM: { double y = S[--sp].u.num; Val r = v_bool(S[sp-1].u.num <= y); sp--; S[sp++] = r; break; }
        case OP_GT_NUM: { double y = S[--sp].u.num; Val r = v_bool(S[sp-1].u.num >  y); sp--; S[sp++] = r; break; }
        case OP_GE_NUM: { double y = S[--sp].u.num; Val r = v_bool(S[sp-1].u.num >= y); sp--; S[sp++] = r; break; }
        case OP_EQ_NUM: { double y = S[--sp].u.num; Val r = v_bool(S[sp-1].u.num == y); sp--; S[sp++] = r; break; }
        case OP_NE_NUM: { double y = S[--sp].u.num; Val r = v_bool(S[sp-1].u.num != y); sp--; S[sp++] = r; break; }

        case OP_ADD: {
            Val y = S[--sp], x = S[--sp];
            if (x.tag == T_NUM && y.tag == T_NUM) { S[sp++] = v_num(x.u.num + y.u.num); }
            else if (x.tag == T_TEXT || y.tag == T_TEXT) {
                char a[2048], b[2048];
                to_text(x, a, sizeof a);
                to_text(y, b, sizeof b);
                char *m = (char *)malloc(strlen(a) + strlen(b) + 1);
                strcpy(m, a); strcat(m, b);
                S[sp++] = v_text(m);
                free(m);
                val_release(x); val_release(y);
            } else {
                raise_err("类型", "不能相加的值");
            }
            break;
        }
        case OP_SUB: { Val y=S[--sp], x=S[--sp]; S[sp++]=v_num(x.u.num-y.u.num); break; }
        case OP_MUL: { Val y=S[--sp], x=S[--sp]; S[sp++]=v_num(x.u.num*y.u.num); break; }
        case OP_DIV: {
            Val y=S[--sp], x=S[--sp];
            if (y.u.num == 0) { runtime_error("除零", "除数不能为零"); break; }
            S[sp++]=v_num(x.u.num/y.u.num);
            break;
        }
        case OP_MOD: {
            Val y = S[--sp], x = S[--sp];
            if (y.u.num == 0) { runtime_error("除零", "取模的除数不能为零"); break; }
            S[sp++] = v_num(fmod(x.u.num, y.u.num));
            break;
        }
        case OP_NEG: S[sp-1].u.num = -S[sp-1].u.num; break;
        case OP_NOT: S[sp-1] = v_bool(!(S[sp-1].tag == T_BOOL ? S[sp-1].u.b : 0)); break;

        case OP_CONCAT: {
            Val y = S[--sp], x = S[--sp];
            char a[2048], b[2048];
            to_text(x, a, sizeof a);
            to_text(y, b, sizeof b);
            char *m = (char *)malloc(strlen(a) + strlen(b) + 1);
            strcpy(m, a); strcat(m, b);
            S[sp++] = v_text(m);
            free(m);
            val_release(x); val_release(y);
            break;
        }

        case OP_EQ: case OP_NE: {
            Val y = S[--sp], x = S[--sp];
            int eq = 0;
            if (x.tag == y.tag) {
                switch (x.tag) {
                case T_NUM:  eq = (x.u.num == y.u.num); break;
                case T_BOOL: eq = (x.u.b == y.u.b); break;
                case T_NIL:  eq = 1; break;
                case T_TEXT:
                    /* 驻留文本：同地址即相等，省掉 strcmp */
                    eq = (x.u.obj == y.u.obj) ? 1
                         : (strcmp(x.u.obj->text, y.u.obj->text) == 0);
                    break;
                default:     eq = (x.u.obj == y.u.obj); break;
                }
            }
            S[sp++] = v_bool(op == OP_EQ ? eq : !eq);
            val_release(x); val_release(y);
            break;
        }
        case OP_LT: { Val y=S[--sp], x=S[--sp]; S[sp++]=v_bool(x.u.num<y.u.num); break; }
        case OP_LE: { Val y=S[--sp], x=S[--sp]; S[sp++]=v_bool(x.u.num<=y.u.num); break; }
        case OP_GT: { Val y=S[--sp], x=S[--sp]; S[sp++]=v_bool(x.u.num>y.u.num); break; }
        case OP_GE: { Val y=S[--sp], x=S[--sp];
            S[sp++]=v_bool(x.u.num>=y.u.num); break; }

        case OP_JMP: {
            int at = pc - 1;
            int32_t d = rd_i32(code, &pc);
            pc = jump_target(at, 5, d);
            break;
        }
        case OP_JMP_IF: case OP_JMP_IFNOT: {
            int at = pc - 1;
            int32_t d = rd_i32(code, &pc);
            Val c = S[--sp];
            int t = (c.tag == T_BOOL) ? c.u.b : (c.tag == T_NUM ? c.u.num != 0 : c.tag != T_NIL);
            int take = (op == OP_JMP_IF) ? t : !t;
            if (take)
                pc = jump_target(at, 5, d);
            break;
        }

        case OP_CALL: {
            uint16_t fi = rd16(code, &pc);
            uint8_t nargs = code[pc++];
            Func *fn = &g_funcs[fi];
            int base = slot_top;
            if (base + fn->n_slots >= MAX_SLOTS) raise_err("越界", "局部变量空间不足");
            for (int i = 0; i < nargs; i++) L[base + i] = S[sp - nargs + i];
            for (int i = nargs; i < fn->n_slots; i++) L[base + i] = v_nil();
            slot_top += fn->n_slots;
            sp -= nargs;
            g_ffunc[g_frame_top] = fi;
            g_fbase[g_frame_top] = base;
            g_fret[g_frame_top] = pc;
            /* 记录调用者的值栈顶：函数体内 sp 归零，返回时恢复到这里 */
            /* 记录调用者栈顶：函数体内的临时值从 sp_base 开始增长，
               返回时回退到这里并把返回值写在上面。
               既保留调用者已有的中间结果，又不会让递归把栈撑爆。 */
            /* sp 此时已减去 nargs，它就是调用者消耗参数后的栈顶：
               函数返回时把结果放这里，调用者的中间值也都在它下面。 */
            g_fsp[g_frame_top] = sp;
            g_fcode[g_frame_top] = code;
            g_fcode_len[g_frame_top] = code_len;
            g_frame_top++;
            code = fn->code;
            code_len = fn->code_len;
            pc = 0;
            break;
        }

        case OP_RET: case OP_RET_NIL: {
            if (op == OP_RET && sp < 1)
                raise_err("未定义", "内部错误：返回时值栈为空");
            Val ret = (op == OP_RET) ? S[--sp] : v_nil();
            g_frame_top--;
            /* 恢复调用者的值栈：函数体内压入的临时值全部丢弃，
               回到调用者原始栈顶（其中保留了它自己的中间结果） */
            sp = g_fsp[g_frame_top];
            int base = g_fbase[g_frame_top];
            for (int i = base; i < slot_top; i++) val_release(L[i]);
            slot_top = base;
            if (g_frame_top == 0) {
                task->result = ret;      /* 主任务返回值：显式记录 */
                goto task_done;
            }
            /* 恢复到「被中断帧自己」的代码：g_fcode[f] 记录的是帧 f 的
               调用者代码，所以当前帧 f 的代码要用 g_fcode[f] 的上一条关系推不出来——
               改为直接用 g_ffunc 反查（这是唯一可靠来源）。 */
            {
                int cur = (int)g_ffunc[g_frame_top - 1];
                code = (cur >= 0) ? g_funcs[cur].code : g_entry;
                code_len = (cur >= 0) ? g_funcs[cur].code_len : g_entry_len;
            }
            pc = g_fret[g_frame_top];
            /* 入口帧的 base 是 0，但入口本身占用 g_entry_slots 个槽位；
               不能把 slot_top 退到 0，否则下一次 CALL 会覆盖入口变量。 */
            if (g_frame_top == 0) slot_top = g_entry_slots;
            S[sp++] = ret;
            break;
        }

        case OP_MAKE_SEQ: {
            uint16_t n = rd16(code, &pc);
            Obj *o = seq_new();
            o->items = (Val *)calloc(n ? n : 1, sizeof(Val));
            o->n = n;
            for (uint16_t i = 0; i < n; i++) o->items[i] = S[sp - n + i];
            sp -= n;
            S[sp++] = v_obj(o);
            break;
        }

        case OP_MAKE_MAP: {
            uint16_t n = rd16(code, &pc);
            Obj *o = obj_new(T_MAP);
            for (uint16_t i = 0; i < n; i++) {
                Val k = S[sp - n*2 + i*2];
                Val v = S[sp - n*2 + i*2 + 1];
                char kb[512];
                to_text(k, kb, sizeof kb);
                val_retain(v);
                map_set(o, kb, v);
                val_release(k);
            }
            sp -= n * 2;
            S[sp++] = v_obj(o);
            break;
        }

        case OP_MAKE_REC: {
            uint16_t ri = rd16(code, &pc);
            uint8_t nf = code[pc++];
            RecordDef *rd = &g_recs[ri];
            Obj *o = obj_new(T_REC);
            o->rec_name = rd->name;
            o->rec_index = ri;
            for (uint8_t i = 0; i < nf; i++) {
                Val v = S[sp - nf + i];
                val_retain(v);
                map_set(o, rd->fields[i], v);
            }
            sp -= nf;
            S[sp++] = v_obj(o);
            break;
        }

        case OP_FIELD_SET: {
            uint32_t ni = rd32(code, &pc);
            Val v = S[--sp];
            Val base = S[--sp];
            const char *name = g_pool[ni].v.text;
            if (base.tag == T_REC || base.tag == T_MAP) {
                map_set(base.u.obj, name, v);
            } else {
                runtime_error("类型", "该值没有可写字段");
            }
            val_release(v);
            val_release(base);
            break;
        }

        case OP_FIELD_GET: {
            uint32_t ni = rd32(code, &pc);
            Val base = S[--sp];
            const char *name = g_pool[ni].v.text;
            if (base.tag == T_REC || base.tag == T_MAP) {
                S[sp++] = map_get(base.u.obj, name);
                val_retain(S[sp-1]);
            } else {
                raise_err("类型", "该值没有字段");
            }
            val_release(base);
            break;
        }

        case OP_INDEX_GET: {
            Val i = S[--sp], base = S[--sp];
            Val out = v_nil();
            if (base.tag == T_SEQ) {
                int n = (int)i.u.num;
                if (n < 0) n += (int)base.u.obj->n;   /* 支持负下标 */
                if (n < 0 || n >= (int)base.u.obj->n) {
                    runtime_error("越界", "下标超出范围");
                } else {
                    out = base.u.obj->items[n];
                    val_retain(out);
                }
            } else if (base.tag == T_MAP || base.tag == T_REC) {
                char kb[512]; to_text(i, kb, sizeof kb);
                out = map_get(base.u.obj, kb);
                val_retain(out);
            } else {
                raise_err("类型", "该值不支持下标访问");
            }
            val_release(i);
            val_release(base);
            S[sp++] = out;
            break;
        }

        case OP_INDEX_SET: {
            /* 栈顺序：容器、下标、新值
               写时复制：序列被多处引用时必须先复制再写，
               否则 `变量 b = a; b[0] = x` 会污染 a——
               这违背衡语的值语义，是比性能更严重的问题。 */
            Val nv = S[--sp], i = S[--sp], base = S[--sp];
            if (base.tag == T_SEQ) {
                int n = (int)i.u.num;
                /* 通用路径拿不到目标槽位，无法把复制品写回变量；
                   这里直接改——真正的写时复制由 SLOT_INDEX_STORE 负责。 */
                if (n < 0) n += (int)base.u.obj->n;
                if (n < 0 || n >= (int)base.u.obj->n) {
                    runtime_error("越界", "下标赋值超出范围");
                } else {
                    val_release(base.u.obj->items[n]);
                    base.u.obj->items[n] = nv;
                    val_retain(nv);
                }
            } else if (base.tag == T_MAP || base.tag == T_REC) {
                char kb[512]; to_text(i, kb, sizeof kb);
                map_set(base.u.obj, kb, nv);
            } else {
                raise_err("类型", "该值不支持下标赋值");
            }
            val_release(nv);
            val_release(i);
            val_release(base);
            break;
        }

        case OP_LEN: {
            Val v = S[--sp];
            double n = 0;
            if (v.tag == T_TEXT) n = v.u.obj->len;
            else if (v.tag == T_SEQ || v.tag == T_MAP || v.tag == T_REC) n = v.u.obj->n;
            val_release(v);
            S[sp++] = v_num(n);
            break;
        }

        case OP_PRINT: {
            /* 单位索引直接编码在指令里（不再按输出顺序对位） */
            uint32_t ui = rd32(code, &pc);
            Val v = S[--sp];
            const char *u = (ui < g_pool_n && g_pool[ui].v.text) ? g_pool[ui].v.text : "";
            if (v.tag == T_NUM && u && u[0]) {
                char nb[512];
                fmt_num(v.u.num, nb, sizeof nb);
                printf("%s%s\n", nb, u);
            } else {
                print_val(v);
            }
            val_release(v);
            break;
        }

        case OP_THROW: {
            uint32_t ci = rd32(code, &pc);
            Val m = S[--sp];
            char mb[1024]; to_text(m, mb, sizeof mb);
            val_release(m);
            snprintf(g_err_cat, sizeof g_err_cat, "%s",
                     g_pool[ci].v.text ? g_pool[ci].v.text : "未定义");
            snprintf(g_err_msg, sizeof g_err_msg, "%s", mb);
            if (g_handler_n == 0) raise_err(g_err_cat, g_err_msg);
            g_err_pending = 1;
            break;
        }

        case OP_BUILTIN: {
            uint8_t bi = code[pc++];
            uint8_t n = code[pc++];
            uint32_t unit_idx = rd32(code, &pc);
            Val a = S[sp - 1];
            if (bi == 1) {  /* 化为数 */
                if (a.tag == T_NUM) { sp -= n; S[sp++] = a; }
                else if (a.tag == T_TEXT) { sp -= n; S[sp++] = v_num(atof(a.u.obj->text)); }
                else raise_err("类型", "无法化为数");
            } else if (bi == 2) {  /* 长度 */
                double L2 = (a.tag == T_TEXT) ? a.u.obj->len
                          : (a.tag == T_SEQ || a.tag == T_MAP) ? a.u.obj->n : 0;
                sp -= n;
                val_release(a);
                S[sp++] = v_num(L2);
            } else {  /* 化成文 */
                char tb[2048];
                if (a.tag == T_NUM && unit_idx < g_pool_n && g_pool[unit_idx].v.text
                        && g_pool[unit_idx].v.text[0]) {
                    char nb[512];
                    fmt_num(a.u.num, nb, sizeof nb);
                    snprintf(tb, sizeof tb, "%s%s", nb, g_pool[unit_idx].v.text);
                } else {
                    to_text(a, tb, sizeof tb);
                }
                sp -= n;
                val_release(a);
                S[sp++] = v_text(tb);
            }
            break;
        }

        case OP_CAP_CHECK: {
            uint32_t ci = rd32(code, &pc);
            const char *cap = g_pool[ci].v.text;
            if (g_sandbox_on) {
                int ok = 0;
                for (int i = 0; i < g_caps_n; i++)
                    if (strcmp(g_caps[i], cap) == 0) { ok = 1; break; }
                if (!ok) {
                    char msg[512];
                }
            }
            break;
        }

        case OP_SANDBOX_ENTER:
            g_cap_stack[g_cap_stack_n++] = g_sandbox_on ? -(g_caps_n + 1) : 0;
            g_sandbox_on = 1;
            g_caps_n = 0;
            break;

        case OP_SANDBOX_EXIT: {
            int mark = g_cap_stack[--g_cap_stack_n];
            if (mark == 0) { g_sandbox_on = 0; g_caps_n = 0; }
            else { g_caps_n = -mark - 1; }
            break;
        }

        case OP_TRY_BEGIN: {
            /* pc 已指向第一个操作数，所以本指令起始在 pc-1 */
            int insn_at = pc - 1;
            int32_t cp_rel = rd_i32(code, &pc);
            int32_t fp_rel = rd_i32(code, &pc);
            int cp = (cp_rel < 0) ? -1 : jump_target(insn_at, 9, cp_rel);
            int fp = (fp_rel < 0) ? -1 : jump_target(insn_at, 9, fp_rel);
            if (g_handler_n >= MAX_HANDLERS) raise_err("越界", "异常处理器嵌套过深");
            Handler *h = &g_handlers[g_handler_n++];
            h->catch_pc = cp;
            h->finally_pc = fp;
            h->frame_depth = g_frame_top;
            h->slot_top = slot_top;
            h->stack_top = sp;
            break;
        }

        case OP_TRY_END:
            /* 正常退出 try 区：撤销本处理器 */
            if (g_handler_n > 0) g_handler_n--;
            break;

        case OP_END_TRY:
            /* catch 块结束：处理器已在分派时撤销，这里不做任何事。
               （重复撤销会让外层处理器被误删，导致嵌套 try 失效） */
            break;

        case OP_SPAWN: {
            /* 真并发：创建任务并在新线程执行；实参深拷贝给子任务。 */
            uint16_t fi = rd16(code, &pc);
            uint8_t nargs = code[pc++];
            uint16_t mbslot = rd16(code, &pc);
            if (g_trace) fprintf(stderr, "  SPAWN fi=%u nargs=%u sp=%d t0=%d t1=%d\n",
                                 fi, nargs, sp,
                                 nargs>=1?S[sp-nargs].tag:-1,
                                 nargs>=2?S[sp-nargs+1].tag:-1);
            Task *child = task_create(fi, &S[sp - nargs], nargs, task->group);
            if (child) task_add_child(task, child);
            if (child) {
                snprintf(child->mailbox, sizeof child->mailbox, "#slot%u", mbslot);
                pthread_create(&child->thread, NULL, task_thread, child);
            }
            sp -= nargs;
            break;
        }

        case OP_RECV: {
            /* 阻塞取信箱：为空则等待（cond_wait 让出 CPU，不空转） */
            uint16_t slot = rd16(code, &pc);
            char mbn[64];
            snprintf(mbn, sizeof mbn, "#slot%u", slot);
            S[sp++] = mailbox_take(mbn);
            break;
        }

        case OP_JOIN: {
            /* 等待本任务发起的所有子任务完成：保证 同时 块结束时不残留线程。
               不依赖 group id——「谁发起就等谁」更直接，也不会与嵌套块混淆。 */
            uint8_t n = code[pc++];
            (void)n;
            for (int i = 0; i < task->n_children; i++) {
                Task *c = task->children[i];
                while (!c->done) sched_yield();
            }
            task->n_children = 0;   /* 本组已汇合，清空以免影响后续 JOIN */
            break;
        }

        case OP_AWAIT: {
            /* 发起并立即等待：内部等价于 spawn + recv */
            uint16_t fi = rd16(code, &pc);
            uint8_t nargs = code[pc++];
            (void)rd16(code, &pc);   /* 信箱槽位：等待完成 不需要 */
            Task *child = task_create(fi, &S[sp - nargs], nargs, -1);
            sp -= nargs;
            if (child) {
                pthread_create(&child->thread, NULL, task_thread, child);
                while (!child->done) sched_yield();
                if (child->failed) raise_err(child->err_cat, child->err_msg);
                S[sp++] = child->result;
            } else {
                S[sp++] = v_nil();
            }
            break;
        }

        case OP_AUTHORIZE: {
            uint32_t ci = rd32(code, &pc);
            if (g_sandbox_on && g_caps_n < 64) {
                g_caps[g_caps_n++] = g_pool[ci].v.text;
            }
            break;
        }

        case OP_CALL_STD: {
            /* 标准库：统一交给 std_call（与 stdlib2.py 一一对应） */
            uint32_t ni = rd32(code, &pc);
            uint8_t n = code[pc++];
            const char *name = g_pool[ni].v.text;
            Val *args = &S[sp - n];
            Val r = std_call(name, args, n);
            for (int i = 0; i < n; i++) val_release(args[i]);
            sp -= n;
            S[sp++] = r;
            break;
        }

        case OP_CALL_HOST: {
            raise_err("未定义",
                "CALL_HOST 已被 FFI 取代（旧字节码请用当前版本重新编译）");
            break;
        }

        case OP_CALL_FFI: {
            uint32_t ni = rd32(code, &pc);
            uint8_t n = code[pc++];
            const char *full_name = g_pool[ni].v.text;
            Val *args = &S[sp - n];
            Val r = ffi_call(full_name, args, n);
            for (int i = 0; i < n; i++) val_release(args[i]);
            sp -= n;
            S[sp++] = r;
            break;
        }

        case OP_CALL_TOOL: {
            /* 内置工具的默认实现：返回 {状态, 内容, 长度} 形态的映射 */
            uint32_t ni = rd32(code, &pc);
            uint8_t n = code[pc++];
            const char *name = g_pool[ni].v.text;
            char target[512] = "";
            /* 实参在栈顶最后 n 个：必须在 sp -= n 之前取 */
            int ms = 0;
            if (n > 1) {
                Val av = S[sp - 1];
                if (av.tag == T_NUM) ms = (int)av.u.num;
                else if (av.tag == T_TEXT) ms = atoi(av.u.obj->text);
            }
            if (n > 0) to_text(S[sp - n], target, sizeof target);

            /* ---- 真实文件 IO ----
               早期这里是桩：只拼一个「读取:xxx」的字符串返回，
               根本没碰文件系统。做工具时才发现写进去的文件是空的。
               现在按真实语义实现。 */
            /* ---- 终端.读键 ---- 读一个按键，返回键名（"上"/"下"/"回车"/"q"…） ----
               TUI 必须有「非行模式」的单键读取，否则交互极其笨重。
               用 termios 临时关掉行缓冲与回显。 */
            if (strcmp(name, "终端.读键") == 0) {
                sp -= n;
                struct termios oldt, newt;
                int has_tty = (tcgetattr(STDIN_FILENO, &oldt) == 0);
                char out[16] = "";
                if (has_tty) {
                    newt = oldt;
                    newt.c_lflag &= (unsigned)~(ICANON | ECHO);
                    newt.c_cc[VMIN] = 1;
                    newt.c_cc[VTIME] = 0;
                    tcsetattr(STDIN_FILENO, TCSANOW, &newt);
                }
                unsigned char c = 0;
                ssize_t got = read(STDIN_FILENO, &c, 1);
                if (got == 1) {
                    if (c == 27) {
                        /* 方向键：ESC [ A/B/C/D */
                        unsigned char seq[2] = {0, 0};
                        if (read(STDIN_FILENO, &seq[0], 1) == 1 && seq[0] == '[') {
                            if (read(STDIN_FILENO, &seq[1], 1) == 1) {
                                if (seq[1] == 'A') snprintf(out, sizeof out, "上");
                                else if (seq[1] == 'B') snprintf(out, sizeof out, "下");
                                else if (seq[1] == 'C') snprintf(out, sizeof out, "右");
                                else if (seq[1] == 'D') snprintf(out, sizeof out, "左");
                                else snprintf(out, sizeof out, "ESC");
                            } else snprintf(out, sizeof out, "ESC");
                        } else snprintf(out, sizeof out, "ESC");
                    } else if (c == 10 || c == 13) {
                        snprintf(out, sizeof out, "回车");
                    } else if (c == 3) {
                        snprintf(out, sizeof out, "中断");
                    } else if (c < 128) {
                        snprintf(out, sizeof out, "%c", c);
                    } else {
                        snprintf(out, sizeof out, "其它");
                    }
                }
                if (has_tty) tcsetattr(STDIN_FILENO, TCSANOW, &oldt);
                S[sp++] = v_text(out);
                break;
            }

            /* ---- 系统.跑命令 命令 ---- 跑外部命令，返回 {状态, 输出, 错误} ----
               用途：衡语写的 IDE 需要调用编译器本身来执行用户代码。
               VM 内部不含编译器（编译器在另一侧），所以走子进程。 */
            if (strcmp(name, "系统.跑命令") == 0) {
                char cmd[4096] = "";
                if (n >= 1) to_text(S[sp - n], cmd, sizeof cmd);
                sp -= n;
                Obj *o = obj_new(T_MAP);
                FILE *fp = popen(cmd, "r");
                if (!fp) {
                    map_set(o, "状态", v_num(-1));
                    map_set(o, "输出", v_text(""));
                    S[sp++] = v_obj(o);
                    break;
                }
                size_t cap = 65536, used = 0;
                char *buf = (char *)malloc(cap);
                size_t got;
                while ((got = fread(buf + used, 1, cap - used - 1, fp)) > 0) {
                    used += got;
                    if (used + 1024 >= cap) {
                        cap *= 2;
                        buf = (char *)realloc(buf, cap);
                    }
                }
                buf[used] = 0;
                int st = pclose(fp);
                map_set(o, "状态", v_num((double)st));
                map_set(o, "输出", v_text_n(buf, (uint32_t)used));
                free(buf);
                S[sp++] = v_obj(o);
                break;
            }

            /* ---- 服务.启动 端口 ---- */
            if (strcmp(name, "服务.启动") == 0) {
                int port = 0;
                if (n >= 1 && S[sp - 1].tag == T_NUM) port = (int)S[sp - 1].u.num;
                sp -= n;
                if (port <= 0 || port > 65535) { S[sp++] = v_bool(0); break; }
                int fd = socket(AF_INET, SOCK_STREAM, 0);
                if (fd < 0) { S[sp++] = v_bool(0); break; }
                int one = 1;
                setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
                struct sockaddr_in addr;
                memset(&addr, 0, sizeof addr);
                addr.sin_family = AF_INET;
                addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);   /* 只绑本地 */
                addr.sin_port = htons((uint16_t)port);
                if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0
                    || listen(fd, 16) != 0) {
                    close(fd);
                    S[sp++] = v_bool(0);
                    break;
                }
                g_srv_fd = fd;
                S[sp++] = v_bool(1);
                break;
            }

            /* ---- 服务.待请求 ---- 阻塞等一个请求，返回 {路径, 方法, 正文} ---- */
            if (strcmp(name, "服务.待请求") == 0) {
                sp -= n;
                if (g_srv_fd < 0) { S[sp++] = v_nil(); break; }
                struct sockaddr_in cli;
                socklen_t cl = sizeof cli;
                int cfd = accept(g_srv_fd, (struct sockaddr *)&cli, &cl);
                if (cfd < 0) { S[sp++] = v_nil(); break; }
                char req[16384];
                ssize_t got = recv(cfd, req, sizeof req - 1, 0);
                if (got <= 0) { close(cfd); S[sp++] = v_nil(); break; }
                req[got] = 0;
                /* 解析首行：METHOD PATH HTTP/1.1 */
                char method[16] = "GET", path[1024] = "/";
                sscanf(req, "%15s %1023s", method, path);
                /* 正文：空行之后 */
                char *body = strstr(req, "\r\n\r\n");
                body = body ? body + 4 : (char *)"";
                Obj *o = obj_new(T_MAP);
                map_set(o, "方法", v_text(method));
                map_set(o, "路径", v_text(path));
                map_set(o, "正文", v_text(body));
                map_set(o, "连接", v_num((double)cfd));
                S[sp++] = v_obj(o);
                break;
            }

            /* ---- 服务.回复 连接号 状态码 内容类型 正文 ---- */
            if (strcmp(name, "服务.回复") == 0) {
                int cfd = -1, status = 200;
                const char *ctype = "text/html; charset=utf-8";
                const char *body = "";
                if (n >= 1 && S[sp - n].tag == T_NUM) cfd = (int)S[sp - n].u.num;
                if (n >= 2 && S[sp - n + 1].tag == T_NUM) status = (int)S[sp - n + 1].u.num;
                if (n >= 3 && S[sp - n + 2].tag == T_TEXT) ctype = S[sp - n + 2].u.obj->text;
                if (n >= 4 && S[sp - n + 3].tag == T_TEXT) body = S[sp - n + 3].u.obj->text;
                size_t blen = (n >= 4 && S[sp - n + 3].tag == T_TEXT)
                              ? S[sp - n + 3].u.obj->len : 0;
                int ok = 0;
                if (cfd >= 0) {
                    char head[512];
                    int hl = snprintf(head, sizeof head,
                        "HTTP/1.1 %d OK\r\nContent-Type: %s\r\n"
                        "Content-Length: %zu\r\nAccess-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n\r\n",
                        status, ctype, blen);
                    ok = (send(cfd, head, (size_t)hl, 0) == hl);
                    if (blen) ok = ok && (send(cfd, body, blen, 0) == (ssize_t)blen);
                    close(cfd);
                }
                sp -= n;
                S[sp++] = v_bool(ok);
                break;
            }

            /* ---- 系统.可执行文件 ---- 当前解释器/编译器自身的路径 ----
               衡语 IDE 要用它来调编译器执行用户代码。 */
            if (strcmp(name, "系统.可执行文件") == 0) {
                sp -= n;
                const char *self = getenv("HENG_SELF");
                if (self && *self) {
                    S[sp++] = v_text(self);
                } else {
                    char exe[PATH_MAX] = "";
                    heng_self_path(exe, sizeof exe);
                    S[sp++] = v_text(exe);
                }
                break;
            }

            if (strcmp(name, "系统.脚本目录") == 0) {
                sp -= n;
                /* 当前字节码文件所在目录（无目录部分时返回 "."） */
                char dir[1024] = ".";
                /* 优先用 HENG_SCRIPT（原始脚本路径），因为 CLI 可能把字节码放进临时目录 */
                const char *src_path = getenv("HENG_SCRIPT");
                if (!src_path || !*src_path) src_path = (g_argc >= 2 && g_argv) ? g_argv[1] : NULL;
                if (src_path) {
                    snprintf(dir, sizeof dir, "%s", src_path);
                    char *slash = strrchr(dir, '/');
                    if (slash) {
                        if (slash == dir) slash[1] = 0;   /* 根目录 */
                        else *slash = 0;
                    } else {
                        snprintf(dir, sizeof dir, ".");
                    }
                }
                S[sp++] = v_text(dir);
                break;
            }

            if (strcmp(name, "系统.参数") == 0) {
                sp -= n;
                Obj *arr = seq_new();
                /* argv[2..] 是用户参数（argv[1] 是字节码路径） */
                int cnt = (g_argc > 2) ? g_argc - 2 : 0;
                arr->items = (Val *)calloc(cnt ? cnt : 1, sizeof(Val));
                for (int i = 0; i < cnt; i++) {
                    arr->items[arr->n++] = v_text(g_argv[i + 2]);
                }
                arr->cap = (uint32_t)(cnt ? cnt : 1);
                S[sp++] = v_obj(arr);
                break;
            }

            /* ---- 文件.存在 判断路径是否可读 ----
               IDE 打开/保存前必须先判断，否则只能在「读失败」里猜原因。 */
            if (strcmp(name, "文件.存在") == 0) {
                int ok = 0;
                if (n >= 1 && S[sp - n].tag == T_TEXT) ok = access(target, R_OK) == 0;
                sp -= n;
                S[sp++] = v_bool(ok);
                break;
            }

            if (strcmp(name, "文件.读取") == 0) {
                FILE *f = fopen(target, "rb");
                if (!f) {
                    sp -= n;
                    raise_err("未定义", "文件不存在或无法读取");
                }
                fseek(f, 0, SEEK_END);
                long sz = ftell(f);
                if (sz < 0) sz = 0;
                fseek(f, 0, SEEK_SET);
                char *buf = (char *)malloc((size_t)sz + 1);
                size_t got = fread(buf, 1, (size_t)sz, f);
                buf[got] = 0;
                fclose(f);
                Val r = v_text_n(buf, (uint32_t)got);
                free(buf);
                sp -= n;
                S[sp++] = r;
                break;
            }
            if (strcmp(name, "文件.写入") == 0) {
                if (n < 2) {
                    sp -= n;
                    raise_err("类型", "文件.写入 需要（路径, 内容）");
                }
                /* 文本直接取原始字节：走 to_text 的固定缓冲会把中文截在
                   半字节上，写出的文件不是合法 UTF-8——做报表时踩到过。 */
                Val cv = S[sp - 1];
                FILE *f = fopen(target, "wb");
                int ok = 0;
                if (f) {
                    if (cv.tag == T_TEXT && cv.u.obj) {
                        size_t len = cv.u.obj->len;
                        ok = (fwrite(cv.u.obj->text, 1, len, f) == len);
                    } else {
                        char tmp[4096];
                        to_text(cv, tmp, sizeof tmp);
                        size_t len = strlen(tmp);
                        ok = (fwrite(tmp, 1, len, f) == len);
                    }
                    fclose(f);
                }
                sp -= n;
                S[sp++] = v_bool(ok);
                break;
            }

            sp -= n;
            Obj *o = obj_new(T_MAP);
            if (strcmp(name, "工具.慢查询") == 0) {
                struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
                nanosleep(&ts, NULL);
                char c[1024];
                snprintf(c, sizeof c, "%s 完成", target);
                map_set(o, "状态", v_text("完成"));
                map_set(o, "结果", v_text(c));
            } else if (strcmp(name, "工具.查询") == 0) {
                char c[1024];
                snprintf(c, sizeof c, "查询(%s)结果", target);
                map_set(o, "状态", v_text("完成"));
                map_set(o, "内容", v_text(c));
                map_set(o, "长度", v_num((double)strlen(target)));
            } else if (strcmp(name, "工具.清理") == 0) {
                map_set(o, "状态", v_text("已清理"));
            } else if (strcmp(name, "网络.请求") == 0) {
                char c[1024]; snprintf(c, sizeof c, "响应:%s", target);
                map_set(o, "状态", v_text("完成"));
                map_set(o, "内容", v_text(c));
            } else {
                val_release(v_obj(o));
                raise_err("未定义", "未定义工具");
            }
            S[sp++] = v_obj(o);
            break;
        }

        case OP_SELECT: {
            /* select: [a b c] → c ? a : b（栈顶是条件） */
            Val c = S[--sp], bv = S[--sp], av = S[--sp];
            int t = (c.tag == T_BOOL) ? c.u.b : (c.tag != T_NIL);
            Val pick = t ? av : bv;
            val_release(t ? bv : av);
            S[sp++] = pick;
            break;
        }

        case OP_YIELD:
            /* 协作式让出点：多线程下线程本身即可被 OS 抢占，
               这里保留为显式调度提示，语义等价于空操作。 */
            sched_yield();
            break;

        case OP_HALT: return;
        case OP_NOP: break;

        default: {
            char msg[128];
            snprintf(msg, sizeof msg, "原生 VM 尚未实现该指令 0x%02X（pc=%d，帧数 %d，帧函数 %u）",
                     op, pc - 1, g_frame_top,
                     g_frame_top > 0 ? g_ffunc[g_frame_top - 1] : 9999u);
            raise_err("未定义", msg);
        }
        }
    }   /* while 循环结束 */

    task_done:
    /* 收尾：把结果写回任务并通知等待者（必须在 #undef 之前，
       因为这些状态此刻还通过宏别名引用 task 字段） */
    task->sp = sp;
    task->slot_top = slot_top;
    task->pc = pc;
    /* 子任务的返回值放在栈顶（run_task 自然结束）；主任务已在 RET 时记录。 */
    if (task->result.tag == T_NIL && sp > 0) task->result = S[sp - 1];
    #undef g_frame_top
    #undef g_fbase
    #undef g_ffunc
    #undef g_fret
    #undef g_fsp
    #undef g_fcode
    #undef g_fcode_len
    if (task->mailbox[0]) mailbox_push_task(task);
    pthread_mutex_lock(&g_task_lock);
    task->done = 1;
    pthread_mutex_unlock(&g_task_lock);
}

static void *task_thread(void *arg) {
    Task *t = (Task *)arg;
    run_task(t);
    return NULL;
}

int main(int argc, char **argv) {
    g_trace = getenv("HENG_TRACE") != NULL;
    dbg_init();
    setvbuf(stderr, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc < 2) {
        fprintf(stderr, "用法：hengvm <文件.hbc>\n");
        return 2;
    }
    g_argc = argc;
    g_argv = argv;
    load_module(argv[1]);
    g_globals = (Val *)calloc(g_n_globals ? g_n_globals : 1, sizeof(Val));
    for (uint32_t i = 0; i < g_n_globals; i++) g_globals[i] = v_nil();

    /* 主任务：与子任务走完全相同的执行路径，保证语义一致 */
    Task main_task;
    memset(&main_task, 0, sizeof main_task);
    /* 槽位区必须容纳「入口槽位 + 递归调用链上所有帧的槽位」。
       只按 g_entry_slots 分配会让函数调用越界，踩坏相邻堆内存
       （ASan 实测：写坏常量池，导致字段名变空）。 */
    uint32_t slot_cap = g_entry_slots + 4096;
    main_task.slots = (Val *)calloc(slot_cap, sizeof(Val));
    main_task.stack = (Val *)calloc(8192, sizeof(Val));
    main_task.ffunc = (uint32_t *)calloc(256, sizeof(uint32_t));
    main_task.fbase = (uint32_t *)calloc(256, sizeof(uint32_t));
    main_task.fret  = (uint32_t *)calloc(256, sizeof(uint32_t));
    main_task.fsp   = (int *)calloc(256, sizeof(int));
    main_task.fcode = (const uint8_t **)calloc(256, sizeof(uint8_t *));
    main_task.fcode_len = (uint32_t *)calloc(256, sizeof(uint32_t));
    main_task.slot_top = g_entry_slots;
    main_task.code = g_entry;
    main_task.code_len = g_entry_len;
    main_task.group = -1;
    main_task.result = v_nil();
    /* 入口帧：base=0，返回地址 -1 表示「返回即程序结束」 */
    main_task.ffunc[0] = (uint32_t)-1;
    main_task.fbase[0] = 0;
    main_task.fret[0] = 0;
    main_task.fsp[0] = 0;
    main_task.fcode[0] = g_entry;
    main_task.fcode_len[0] = g_entry_len;
    main_task.frame_top = 1;

    run_task(&main_task);
    fflush(stdout);
    return 0;
}
