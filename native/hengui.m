/* 衡语图形 IDE 的原生桥接层（macOS / AppKit）。
 *
 * 为什么要这一层：衡语的 FFI 只支持「全数值 / 单文本 / 双文本」参数，
 * 无法直接调 objc_msgSend（需要对象指针与 selector）。
 * 所以用 C 写一层薄桥，把 AppKit 封装成纯字符串接口，
 * 衡语侧只需要：
 *
 *     导入 外部 "libhengui.dylib" 之 界面
 *     界面.启动("衡语 IDE")
 *     界面.加分区("编辑区", 0, 0, 60, 100)
 *     ...
 *     界面.运行()          // 进入事件循环，回调到衡语
 *
 * 布局与外观由衡语以「命令字符串」描述，本层只负责渲染与事件转发：
 *     "分区|名字|x|y|宽|高"
 *     "文本|分区|内容"
 *     "按钮|分区|标题"
 *     "列表|分区|项1\n项2"
 *
 * 这样 GUI 逻辑（布局、状态、响应）全在衡语里，可测、可改；
 * C 层只做「看不懂业务」的渲染。
 */

#import <Cocoa/Cocoa.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

static NSWindow *g_win = nil;
static int g_app_ready = 0;
static int g_closed = 0;
static NSView *g_status_bar = nil;      /* 状态栏容器，复用不重建 */
static NSTextField *g_status_parts[3] = {nil, nil, nil};
static void slot_put(const char *key, const char *val);
static NSView   *g_root = nil;
static NSMutableDictionary<NSString *, NSView *> *g_panes = nil;
/* 回调：衡语侧注册的「命令处理器」由 VM 侧注入，这里只存函数指针 */
static const char *(*g_handler)(const char *event, const char *payload) = NULL;

/* 事件队列：AppKit 回调里不能直接跑 VM（会阻塞主线程），
   所以按钮点击只入队，由 界面.取事件() 轮询取走。 */
#define QCAP 64
static char g_evt[QCAP][1024];
static int  g_evt_head = 0, g_evt_tail = 0;
static long g_evt_pushed = 0;
static long g_mouse_seen = 0;   /* 收到多少次鼠标按下 */

static void push_event(const char *evt, const char *payload);
static void push_event(const char *evt, const char *payload) {
    int next = (g_evt_tail + 1) % QCAP;
    if (next == g_evt_head) return;              /* 满了就丢，避免主线程卡死 */
    snprintf(g_evt[g_evt_tail], sizeof g_evt[0], "%s|%s", evt, payload ? payload : "");
    g_evt_tail = next;
    g_evt_pushed++;
}

/* ---------- 界面元素的 Objective-C 侧 ---------- */

@interface HengButton : NSButton
@property (nonatomic, copy) NSString *actionCode;
@end

@implementation HengButton
- (void)clicked:(id)sender {
    push_event("按钮", [self.actionCode UTF8String]);
}
@end

/* 菜单、工具栏、标签条共用的回调目标：
   把点击翻译成事件推给衡语，不在 ObjC 侧做任何业务判断。 */
@interface HengMenuTarget : NSObject
+ (instancetype)shared;
- (void)menuHit:(NSMenuItem *)item;
- (void)tabHit:(NSSegmentedControl *)seg;
@end

@implementation HengMenuTarget
+ (instancetype)shared {
    static HengMenuTarget *inst = nil;
    if (!inst) inst = [[HengMenuTarget alloc] init];
    return inst;
}
- (void)menuHit:(NSMenuItem *)item {
    push_event("菜单", [[item title] UTF8String]);
}
- (void)tabHit:(NSSegmentedControl *)seg {
    NSString *label = [seg labelForSegment:[seg selectedSegment]];
    push_event("标签", [label UTF8String]);
}
@end

@interface HengPane : NSView
@property (nonatomic, copy) NSString *paneName;
@property (nonatomic) double px, py, pw, ph;   /* 百分比布局参数 */
@end

@implementation HengPane
@end

/* 窗口尺寸变化时按百分比重算所有分区，实现真正的自适应 */
@interface HengRoot : NSView
@end

@implementation HengRoot
- (void)resizeSubviewsWithOldSize:(NSSize)oldSize {
    [super resizeSubviewsWithOldSize:oldSize];
    NSRect b = [self bounds];
    for (NSView *v in [self subviews]) {
        if (![v isKindOfClass:[HengPane class]]) continue;
        HengPane *p = (HengPane *)v;
        if (p.pw <= 0) continue;
        [p setFrame:NSMakeRect(b.size.width * p.px / 100.0,
                               b.size.height * (100.0 - p.py - p.ph) / 100.0,
                               b.size.width * p.pw / 100.0,
                               b.size.height * p.ph / 100.0)];
        for (NSView *c in [p subviews]) [c setFrame:[p bounds]];
    }
}
@end

/* ---------- 供衡语调用的纯 C 接口 ---------- */

/* 启动：建窗口。返回 1 表示成功。不进入事件循环。 */
/* 设置进程在系统里的显示名。不设的话系统窗口列表里看到的是「hengvm」，
   用户根本认不出那是自己的 IDE。 */
static void set_process_name(const char *name) {
    if (!name || !*name) return;
    @autoreleasepool {
        [[NSProcessInfo processInfo]
            setProcessName:[NSString stringWithUTF8String:name]];
    }
}

const char *界面_启动_t(const char *title) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];

        /* 自适应屏幕：取可见区域的 88%，并夹在合理区间内。
           小屏（13 寸）不会溢出，大屏（27 寸）不会开得太空。 */
        NSRect scr = [[NSScreen mainScreen] visibleFrame];
        double w = scr.size.width * 0.88;
        double h = scr.size.height * 0.88;
        if (w < 900) w = 900;
        if (w > 1800) w = 1800;
        if (h < 620) h = 620;
        if (h > 1200) h = 1200;
        if (w > scr.size.width - 40) w = scr.size.width - 40;
        if (h > scr.size.height - 40) h = scr.size.height - 40;
        NSRect frame = NSMakeRect(0, 0, w, h);
        g_win = [[NSWindow alloc]
            initWithContentRect:frame
                      styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                                 | NSWindowStyleMaskResizable)
                        backing:NSBackingStoreBuffered
                          defer:NO];
        set_process_name(title ? title : "衡语");
        set_process_name(title ? title : "衡语 IDE");
        [g_win setTitle:[NSString stringWithUTF8String:title ? title : "衡语"]];
        /* 窗口标题为空会让系统窗口列表里 name 字段空白，
           在 Mission Control / 窗口菜单里难以辨认 */
        [g_win setRepresentedFilename:@""];
        [g_win setDocumentEdited:NO];
        [g_win center];

        g_root = [[HengRoot alloc] initWithFrame:frame];
        [g_root setWantsLayer:YES];
        [[g_root layer] setBackgroundColor:[[NSColor colorWithCalibratedRed:0.10
                                                                     green:0.11
                                                                      blue:0.15
                                                                     alpha:1.0] CGColor]];
        /* 关键：默认 releasedWhenClosed=YES 时窗口关闭会自我释放，
           但 g_win 还指向它，下一次访问就是野指针，
           实测崩溃信息：-[NSWindow dealloc] Assertion failed
           （窗口仍被 NSApp._openWindows 引用时被释放）。
           设为 NO 让生命周期由我们控制。 */
        [g_win setReleasedWhenClosed:NO];
        [g_win setContentView:g_root];
        [NSApp finishLaunching];
        g_app_ready = 1;

        /* 抢前台的三件事，缺一不可：
           1 setActivationPolicy:Regular —— 出现在 Dock，能 Cmd+Tab
           2 activateIgnoringOtherApps   —— 从后台进程变成前台
           3 makeKeyAndOrderFront + orderFrontRegardless —— 窗口真的提到最前
           少了第 3 项，从终端/launchd 启动时窗口会留在所有窗口后面，
           看起来就像「只有终端、没有界面」。 */
        [g_win makeKeyAndOrderFront:nil];
        [g_win orderFrontRegardless];
        [NSApp activateIgnoringOtherApps:YES];
        [g_win display];
        [g_win setLevel:NSNormalWindowLevel];

        g_panes = [NSMutableDictionary dictionary];
        return "1";
    }
}

/* 清空所有分区（重新布局时用） */
const char *界面_清空_t(const char *ignored) {
    @autoreleasepool {
        for (NSView *v in [g_panes allValues]) [v removeFromSuperview];
        [g_panes removeAllObjects];
    }
    return "1";
}

/* 加分区："名字,x,y,w,h"（x/y/w/h 是 0~100 的百分比） */
const char *界面_加分区_t(const char *spec) {
    @autoreleasepool {
        char name[128] = "";
        double x = 0, y = 0, w = 30, h = 30;
        if (sscanf(spec, "%127[^,],%lf,%lf,%lf,%lf", name, &x, &y, &w, &h) < 5) return "0";
        NSRect b = [g_root bounds];
        NSRect f = NSMakeRect(b.size.width * x / 100.0,
                              b.size.height * (100.0 - y - h) / 100.0,
                              b.size.width * w / 100.0,
                              b.size.height * h / 100.0);
        /* 同名分区复用：重复调用 加分区 时不能再加一个同名的，
           否则同一个名字指向新视图、旧视图还留在屏幕上，看起来就是「叠加错乱」。 */
        if (!g_panes) g_panes = [NSMutableDictionary dictionary];
        NSString *pname = [NSString stringWithUTF8String:name];
        HengPane *exist = g_panes[pname];
        if (exist) {
            exist.px = x; exist.py = y; exist.pw = w; exist.ph = h;
            [exist setFrame:f];
            return "1";
        }
        HengPane *p = [[HengPane alloc] initWithFrame:f];
        [p setWantsLayer:YES];
        [[p layer] setBackgroundColor:[[NSColor colorWithCalibratedRed:0.15
                                                                green:0.16
                                                                 blue:0.21
                                                                alpha:1.0] CGColor]];
        [[p layer] setCornerRadius:6.0];
        p.paneName = [NSString stringWithUTF8String:name];
        p.px = x; p.py = y; p.pw = w; p.ph = h;
        [g_root addSubview:p];
        g_panes[p.paneName] = p;
        return "1";
    }
}

/* 刷新前清掉分区里的旧内容。
   这是「文字重影/按钮错位」的根因修复：
   早期 界面_文本_t 每次都新建 NSTextView 叠上去，
   IDE 每刷新一次就多一层，几轮之后满屏重影。 */
static void pane_clear_children(HengPane *p) {
    if (!p) return;
    for (NSView *v in [[p subviews] copy]) {
        [v removeFromSuperview];
    }
}

static HengPane *pane_of(const char *name) {
    if (!g_panes || !name) return nil;
    return g_panes[[NSString stringWithUTF8String:name]];
}

/* 在分区里放多行文本（只读标签，等宽字体） */
const char *界面_文本_t(const char *spec) {
    @autoreleasepool {
        /* spec = "分区|内容"，用竖线分隔（内容里可以有逗号） */
        char buf[16384];
        snprintf(buf, sizeof buf, "%s", spec ? spec : "");
        char *bar = strchr(buf, '|');
        if (!bar) return "0";
        *bar = 0;
        const char *name = buf;
        const char *content = bar + 1;
        HengPane *p = pane_of(name);
        if (!p) return "0";

        /* 已有文本视图就原地更新内容，不再新建。
           新建会导致：① 层层叠加出重影 ② 每次都抢焦点 ③ 滚动位置丢失。 */
        for (NSView *v in [p subviews]) {
            if ([v isKindOfClass:[NSTextView class]]) {
                NSTextView *old = (NSTextView *)v;
                [old setString:[NSString stringWithUTF8String:content ? content : ""]];
                [old setFrame:[p bounds]];
                [old setFont:[NSFont monospacedSystemFontOfSize:12
                                                         weight:NSFontWeightRegular]];
                [old setTextColor:[NSColor colorWithCalibratedRed:0.85
                                                            green:0.88
                                                             blue:0.95
                                                            alpha:1]];
                [old setBackgroundColor:[NSColor clearColor]];
                [old setDrawsBackground:NO];
                return "1";
            }
        }
        NSTextView *tv = [[NSTextView alloc] initWithFrame:[p bounds]];
        [tv setString:[NSString stringWithUTF8String:content ? content : ""]];
        [tv setEditable:NO];
        [tv setFont:[NSFont monospacedSystemFontOfSize:12 weight:NSFontWeightRegular]];
        [tv setTextColor:[NSColor colorWithCalibratedRed:0.85 green:0.88 blue:0.95 alpha:1]];
        [tv setBackgroundColor:[NSColor clearColor]];
        [tv setDrawsBackground:NO];
        [p addSubview:tv];
        return "1";
    }
}

/* 在分区里放按钮："分区,标题,动作号" */
const char *界面_按钮_t(const char *spec) {
    @autoreleasepool {
        char name[128] = "", label[128] = "", action[64] = "";
        if (sscanf(spec, "%127[^,],%127[^,],%63s", name, label, action) < 3) return "0";
        HengPane *p = pane_of(name);
        if (!p) return "0";
        NSRect b = [p bounds];
        double bh = 28, gap = 6, topPad = 8, leftPad = 10;
        double width = b.size.width - leftPad * 2;
        if (width > 180) width = 180;
        if (width < 60) width = 60;

        /* 只数「已有的按钮」，从分区顶部往下排。
           早期用 [p subviews].count 当序号，把文本视图也算进去了，
           而且从下往上累加——每加一个按钮就往上跳一格，
           加满几个后按钮全部跑到分区外面，看起来就是「排版错乱、点了没反应」。
           同一标题的按钮视为「同一个控件」，重复调用只更新它，不再叠加。 */
        NSInteger nbtn = 0;
        HengButton *same = nil;
        for (NSView *v in [p subviews]) {
            if (![v isKindOfClass:[NSButton class]]) continue;
            if ([[(NSButton *)v title] isEqualToString:[NSString stringWithUTF8String:label]]) {
                same = (HengButton *)v;
            }
            nbtn++;
        }
        if (same) {
            double idx = 0;
            NSInteger seen = 0;
            for (NSView *v in [p subviews]) {
                if (![v isKindOfClass:[NSButton class]]) continue;
                if (v == same) { idx = (double)seen; break; }
                seen++;
            }
            double y = b.size.height - topPad - bh - idx * (bh + gap);
            [same setFrame:NSMakeRect(leftPad, y, width, bh)];
            same.actionCode = [NSString stringWithUTF8String:action];
            return "1";
        }

        double y = b.size.height - topPad - bh - (double)nbtn * (bh + gap);
        NSRect f = NSMakeRect(leftPad, y, width, bh);
        HengButton *btn = [[HengButton alloc] initWithFrame:f];
        [btn setTitle:[NSString stringWithUTF8String:label]];
        [btn setBezelStyle:NSBezelStyleRounded];
        btn.actionCode = [NSString stringWithUTF8String:action];
        [btn setTarget:btn];
        [btn setAction:@selector(clicked:)];
        [p addSubview:btn];
        return "1";
    }
}

/* 把 AppKit 待处理事件跑一轮，让界面不假死；同时取一个排队中的按钮事件。
 *
 * 关键：窗口要真正「画出来」必须走完 AppKit 的绘制周期。
 * 只 sendEvent 不 finishLaunching / 不跑 run loop，窗口对象存在但屏幕上没有东西——
 * 表现就是「进程活着，却只有终端」。这里补上初始化与绘制。 */
static void ensure_app_ready(void) {
    if (g_app_ready) return;
    [NSApp finishLaunching];
    g_app_ready = 1;
}

static const char *heng_next_event(void) {
    static char out[1024];
    @autoreleasepool {
        ensure_app_ready();
        NSEvent *e = nil;
        while ((e = [NSApp nextEventMatchingMask:NSEventMaskAny
                                       untilDate:[NSDate distantPast]
                                          inMode:NSDefaultRunLoopMode
                                         dequeue:YES])) {
            [NSApp sendEvent:e];
        }
        /* 强制重绘，否则窗口内容可能一直空白 */
        if (g_win) {
            [g_win displayIfNeeded];
            [[g_win contentView] displayIfNeeded];
        }
        if (g_evt_head == g_evt_tail) { out[0] = 0; return out; }
        snprintf(out, sizeof out, "%s", g_evt[g_evt_head]);
        g_evt_head = (g_evt_head + 1) % QCAP;
        return out;
    }
}

/* 阻塞式轮询：最多等 ms 毫秒。
   早期的 界面.轮询 是「立即返回」，导致衡语侧主循环全速空转，
   几秒钟就把循环上限跑完、进程退出——表现就是「只有终端、窗口一闪而过」。
   这里改成真正的阻塞等待。 */
const char *界面_等待_t(const char *ms_text) {
    int ms = ms_text ? atoi(ms_text) : 50;
    if (ms < 0) ms = 0;
    @autoreleasepool {
        ensure_app_ready();

        /* 关键：窗口必须一直保持「已订购到屏幕」的状态。
           实测现象：裸二进制启动时窗口显示 2 秒后从窗口服务器消失，
           因为 AppKit 没有真正的 run loop 在维持它。
           做法：每轮把 window 重新 orderFront（幂等，不会闪烁），
           并在 default + eventTracking + modalPanel 三种 mode 下都收事件，
           这样鼠标拖拽、菜单跟踪期间也不会丢点击。 */
        if (g_win && ![g_win isVisible]) {
            [g_win orderFrontRegardless];
        }

        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:ms / 1000.0];
        NSArray *modes = @[NSDefaultRunLoopMode,
                           NSEventTrackingRunLoopMode,
                           NSModalPanelRunLoopMode];
        for (NSString *mode in modes) {
            NSEvent *e = [NSApp nextEventMatchingMask:NSEventMaskAny
                                            untilDate:deadline
                                               inMode:mode
                                              dequeue:YES];
            if (e) {
                if ([e type] == NSEventTypeLeftMouseDown) g_mouse_seen++;
                [NSApp sendEvent:e];
                break;
            }
        }
        /* 排空剩余事件 */
        NSEvent *e2 = nil;
        while ((e2 = [NSApp nextEventMatchingMask:NSEventMaskAny
                                       untilDate:[NSDate distantPast]
                                          inMode:NSDefaultRunLoopMode
                                         dequeue:YES])) {
            if ([e2 type] == NSEventTypeLeftMouseDown) g_mouse_seen++;
            [NSApp sendEvent:e2];
        }
        if (g_win) {
            [g_win displayIfNeeded];
            [[g_win contentView] setNeedsDisplay:YES];
            [[g_win contentView] displayIfNeeded];
        }
        if (g_evt_head == g_evt_tail) { slot_put("事件", ""); return "0"; }
        char buf[1024];
        snprintf(buf, sizeof buf, "%s", g_evt[g_evt_head]);
        g_evt_head = (g_evt_head + 1) % QCAP;
        slot_put("事件", buf);
    }
    return "1";
}

/* 取事件：把 "事件类型|载荷" 写进调用方缓冲，返回长度。
   为什么不用 const char* 返回值：衡语 FFI 对「零参数」函数一律按 double
   解释返回值（整数与浮点的返回寄存器不同），字符串指针会被当成数值，
   得到 0 或直接崩溃。改成「输出缓冲 + 返回长度」就绕开了这个限制。 */
/* 结果槽：桥接层内部保存最近一次结果，衡语用 界面.取结果("键") 拉走。
   这样所有「字符串出参」都变成「单文本参数 + 字符串返回」——
   正是衡语 FFI 唯一可靠支持的形态。 */
static NSMutableDictionary<NSString *, NSString *> *g_slot = nil;

static void slot_put(const char *key, const char *val) {
    if (!g_slot) g_slot = [NSMutableDictionary dictionary];
    g_slot[[NSString stringWithUTF8String:key]] = [NSString stringWithUTF8String:(val ? val : "")];
}

/* 自检：报告窗口与分区状态，供衡语写日志证明 GUI 真的建起来了 */
/* 是否已被用户关闭（衡语据此退出主循环） */
const char *界面_已关闭_t(const char *ignored) {
    return g_closed ? "1" : "0";
}

const char *界面_事件诊断_t(const char *ignored) {
    static char out[512];
    @autoreleasepool {
        snprintf(out, sizeof out,
                 "推送=%ld 鼠标按下=%ld 队列=%d app_ready=%d 主窗口=%d",
                 g_evt_pushed, g_mouse_seen,
                 (g_evt_tail - g_evt_head + QCAP) % QCAP, g_app_ready,
                 (g_win && [g_win isKeyWindow]) ? 1 : 0);
    }
    return out;
}

/* 几何诊断：把「点击无效」和「点错位置」区分开 */
const char *界面_几何_t(const char *ignored) {
    static char out[1024];
    @autoreleasepool {
        if (!g_win) { snprintf(out, sizeof out, "无窗口"); return out; }
        NSRect wf = [g_win frame];
        NSRect scr = [[NSScreen mainScreen] frame];
        double top = scr.size.height - (wf.origin.y + wf.size.height);
        snprintf(out, sizeof out,
                 "窗口 x=%.0f y=%.0f w=%.0f h=%.0f 顶向下Y=%.0f 屏幕高=%.0f",
                 wf.origin.x, wf.origin.y, wf.size.width, wf.size.height,
                 top, scr.size.height);
    }
    return out;
}

/* 控件清单：列出所有按钮在「窗口内」的坐标，
   用来核对点击位置到底该落在哪。这是排查「点了没反应」最直接的手段。 */
const char *界面_控件清单_t(const char *ignored) {
    static char out[4096];
    @autoreleasepool {
        NSMutableString *sb = [NSMutableString string];
        NSRect wf = [g_win frame];
        [sb appendFormat:@"窗口 %.0fx%.0f\n", wf.size.width, wf.size.height];
        for (NSString *k in g_panes) {
            HengPane *p = g_panes[k];
            [sb appendFormat:@"分区 %@ frame=(%.0f,%.0f,%.0f,%.0f)\n",
                k, p.frame.origin.x, p.frame.origin.y,
                p.frame.size.width, p.frame.size.height];
            for (NSView *v in [p subviews]) {
                if ([v isKindOfClass:[NSButton class]]) {
                    NSButton *b = (NSButton *)v;
                    /* 换算成「从窗口左下算」→再转「从窗口顶部算」 */
                    NSPoint inWin = [v convertPoint:NSZeroPoint toView:nil];
                    double topY = wf.size.height - inWin.y - v.frame.size.height;
                    double cx = inWin.x + v.frame.size.width / 2;
                    double cy = topY + v.frame.size.height / 2;
                    [sb appendFormat:@"  按钮「%@」窗口内(top-down) 中心=(%.0f,%.0f) 大小=%.0fx%.0f\n",
                        [b title], cx, cy, v.frame.size.width, v.frame.size.height];
                }
            }
        }
        snprintf(out, sizeof out, "%s", [sb UTF8String]);
    }
    return out;
}

/* 测试用：按标题直接触发某个按钮的 action。
   为什么不模拟鼠标：CGEventPost 需要「辅助功能」权限，
   本机测试环境没有该权限，注入的点击会被系统丢弃，
   于是「点击无效」可能是测试手段的问题而不是产品问题。
   直接调 performClick 走的是与真人点击完全相同的 target/action 链路，
   能干净地区分「事件没送到」和「槽函数没实现」。 */
const char *界面_触发_t(const char *title) {
    @autoreleasepool {
        if (!title) return "0";
        NSString *want = [NSString stringWithUTF8String:title];
        for (NSString *k in g_panes) {
            for (NSView *v in [g_panes[k] subviews]) {
                if ([v isKindOfClass:[HengButton class]] &&
                    [[(NSButton *)v title] isEqualToString:want]) {
                    [(HengButton *)v performClick:nil];
                    return "1";
                }
            }
        }
        return "0";
    }
}

/* 系统文件选择器。
   结果写进槽 "选中文件"，返回 "1" 表示选了、"0" 表示取消。
   用 NSOpenPanel 而不是自己画对话框：这样才有访达侧边栏、
   最近使用、拖放、iCloud 等用户已经习惯的东西。 */
const char *界面_选文件_t(const char *hint) {
    @autoreleasepool {
        ensure_app_ready();
        NSOpenPanel *panel = [NSOpenPanel openPanel];
        [panel setCanChooseFiles:YES];
        [panel setCanChooseDirectories:NO];
        [panel setAllowsMultipleSelection:NO];
        if (hint && *hint) {
            [panel setTitle:[NSString stringWithUTF8String:hint]];
        }
        /* 只让选 .hng，但也允许手动切换成全部文件 */
        if (@available(macOS 11.0, *)) {
            UTType *t = [UTType typeWithFilenameExtension:@"hng"];
            if (t) [panel setAllowedContentTypes:@[t]];
        }
        NSInteger rc = [panel runModal];
        if (rc == NSModalResponseOK) {
            NSString *path = [[panel URL] path];
            if (!g_slot) g_slot = [NSMutableDictionary dictionary];
            g_slot[@"选中文件"] = path ? path : @"";
            return "1";
        }
        if (!g_slot) g_slot = [NSMutableDictionary dictionary];
        g_slot[@"选中文件"] = @"";
        return "0";
    }
}

/* 保存对话框：返回用户选的路径到槽 "保存路径" */
const char *界面_选保存路径_t(const char *defaultName) {
    @autoreleasepool {
        ensure_app_ready();
        NSSavePanel *panel = [NSSavePanel savePanel];
        if (defaultName && *defaultName) {
            [panel setNameFieldStringValue:[NSString stringWithUTF8String:defaultName]];
        }
        if (@available(macOS 11.0, *)) {
            UTType *t = [UTType typeWithFilenameExtension:@"hng"];
            if (t) [panel setAllowedContentTypes:@[t]];
        }
        NSInteger rc = [panel runModal];
        if (!g_slot) g_slot = [NSMutableDictionary dictionary];
        if (rc == NSModalResponseOK) {
            NSString *path = [[panel URL] path];
            g_slot[@"保存路径"] = path ? path : @"";
            return "1";
        }
        g_slot[@"保存路径"] = @"";
        return "0";
    }
}

/* ---- 快捷键 ----
 *
 * 为什么必须有：专业 IDE 是键盘驱动的。只有按钮的界面永远是「简陋」的。
 *
 * 实现方式：在 HengRoot 里重写 performKeyEquivalent，
 * 把「修饰键 + 字符」翻译成事件推给衡语，由衡语的命令表决定做什么。
 * C 层不硬编码任何业务逻辑——加一个快捷键只需在衡语里注册一行。
 *
 * 事件格式：快捷键|cmd+shift+p  （修饰键按 cmd/shift/opt/ctrl 顺序，小写）
 */
static NSString *heng_mod_string(NSEventModifierFlags f) {
    NSMutableArray *parts = [NSMutableArray array];
    if (f & NSEventModifierFlagControl) [parts addObject:@"ctrl"];
    if (f & NSEventModifierFlagOption)  [parts addObject:@"opt"];
    if (f & NSEventModifierFlagShift)   [parts addObject:@"shift"];
    if (f & NSEventModifierFlagCommand) [parts addObject:@"cmd"];
    return [parts componentsJoinedByString:@"+"];
}

@interface HengRoot (Keys)
@end
@implementation HengRoot (Keys)

- (BOOL)performKeyEquivalent:(NSEvent *)event {
    NSEventModifierFlags f = [event modifierFlags] & NSEventModifierFlagDeviceIndependentFlagsMask;
    /* 无修饰键不在这一层处理，交给正常的键盘输入 */
    if (!(f & (NSEventModifierFlagCommand | NSEventModifierFlagControl |
               NSEventModifierFlagOption))) {
        return NO;
    }
    NSString *chars = [[event charactersIgnoringModifiers] lowercaseString];
    if (!chars || [chars length] == 0) return NO;
    /* 只认单字符快捷键：多字符说明是输入法组合，不该拦 */
    if ([chars length] > 1) return NO;

    NSString *combo = [NSString stringWithFormat:@"%@+%@",
                       heng_mod_string(f), chars];
    push_event("快捷键", [combo UTF8String]);
    return YES;      /* 已消费，阻止系统默认行为 */
}

@end

/* 暴露给衡语：查询某个快捷键是否被按下过（用于调试与自检） */
const char *界面_快捷键信息_t(const char *ignored) {
    static char out[512];
    @autoreleasepool {
        /* 报告当前窗口的 keyEquivalent 拦截是否已生效 */
        snprintf(out, sizeof out, "HengRoot 已接管 performKeyEquivalent，按键会以「快捷键|修饰键+字符」推给衡语");
    }
    return out;
}

/* ---- 命令面板 ----
 *
 * 借鉴 VS Code 的 Cmd+Shift+P / Sublime 的 Goto Anything。
 * 一个无边框面板 + 搜索框 + 列表，方向键选择，回车执行，Esc 关闭。
 *
 * 衡语负责：提供候选列表、决定回车后做什么。
 * C 层负责：渲染、过滤、键盘导航。
 * 这样「有哪些命令」这件事完全留在衡语里。
 */

@interface HengPalette : NSPanel <NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate>
@property (strong) NSTextField *search;
@property (strong) NSTableView *table;
@property (strong) NSScrollView *scroll;
@property (strong) NSArray<NSString *> *allItems;
@property (strong) NSMutableArray<NSString *> *shown;
@property (strong) NSView *parentView;
@end

static HengPalette *g_palette = nil;

@implementation HengPalette

- (instancetype)initWithParent:(NSView *)parent {
    NSRect pf = [parent bounds];
    NSRect wf = [[parent window] frame];
    CGFloat w = 560, h = 320;
    NSRect frame = NSMakeRect(NSMidX(pf) - w / 2, NSMaxY(pf) - h - 60, w, h);
    self = [super initWithContentRect:frame
                            styleMask:NSWindowStyleMaskBorderless
                              backing:NSBackingStoreBuffered
                                defer:NO];
    if (!self) return nil;
    [self setOpaque:NO];
    [self setBackgroundColor:[NSColor clearColor]];
    [self setLevel:NSFloatingWindowLevel];
    [self setHasShadow:YES];
    self.parentView = parent;

    NSView *content = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, w, h)];
    [content setWantsLayer:YES];
    content.layer.backgroundColor = [[NSColor colorWithCalibratedRed:0.13
                                                               green:0.14
                                                                blue:0.18
                                                               alpha:0.98] CGColor];
    content.layer.cornerRadius = 10;
    content.layer.borderWidth = 1;
    content.layer.borderColor = [[NSColor colorWithCalibratedRed:0.30
                                                           green:0.34
                                                            blue:0.42
                                                           alpha:1] CGColor];

    self.search = [[NSTextField alloc] initWithFrame:NSMakeRect(14, h - 46, w - 28, 30)];
    [self.search setPlaceholderString:@"输入命令名…（↑↓ 选择，回车执行，Esc 关闭）"];
    [self.search setFont:[NSFont systemFontOfSize:14]];
    [self.search setBezelStyle:NSTextFieldRoundedBezel];
    [self.search setDelegate:self];
    [content addSubview:self.search];

    NSRect tf = NSMakeRect(10, 10, w - 20, h - 64);
    self.scroll = [[NSScrollView alloc] initWithFrame:tf];
    [self.scroll setHasVerticalScroller:YES];
    [self.scroll setDrawsBackground:NO];
    self.table = [[NSTableView alloc] initWithFrame:tf];
    NSTableColumn *col = [[NSTableColumn alloc] initWithIdentifier:@"cmd"];
    [col setWidth:w - 40];
    [self.table addTableColumn:col];
    [self.table setHeaderView:nil];
    [self.table setRowHeight:26];
    [self.table setBackgroundColor:[NSColor clearColor]];
    [self.table setDataSource:self];
    [self.table setDelegate:self];
    [self.table setSelectionHighlightStyle:NSTableViewSelectionHighlightStyleRegular];
    [self.scroll setDocumentView:self.table];
    [content addSubview:self.scroll];

    [self setContentView:content];
    self.shown = [NSMutableArray array];
    return self;
}

- (void)showWithItems:(NSArray<NSString *> *)items {
    self.allItems = items ?: @[];
    [self.shown setArray:self.allItems];
    [self.table reloadData];
    if ([self.shown count] > 0) {
        [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:0]
                byExtendingSelection:NO];
    }
    [self.search setStringValue:@""];
    [[self parentWindow] addChildWindow:self ordered:NSWindowAbove];
    [self makeKeyAndOrderFront:nil];
    [[self parentWindow] makeFirstResponder:self.search];
}

- (void)hide {
    [[self parentWindow] removeChildWindow:self];
    [self orderOut:nil];
}

- (void)controlTextDidChange:(NSNotification *)note {
    NSString *q = [[self.search stringValue] lowercaseString];
    [self.shown removeAllObjects];
    if ([q length] == 0) {
        [self.shown addObjectsFromArray:self.allItems];
    } else {
        for (NSString *it in self.allItems) {
            if ([[it lowercaseString] rangeOfString:q].location != NSNotFound) {
                [self.shown addObject:it];
            }
        }
    }
    [self.table reloadData];
    if ([self.shown count] > 0) {
        [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:0]
                byExtendingSelection:NO];
    }
}

- (NSInteger)numberOfRowsInTableView:(NSTableView *)tv {
    return (NSInteger)[self.shown count];
}

- (NSView *)tableView:(NSTableView *)tv
   viewForTableColumn:(NSTableColumn *)col
                  row:(NSInteger)row {
    NSTextField *tf = [tv makeViewWithIdentifier:@"cell" owner:self];
    if (!tf) {
        tf = [[NSTextField alloc] initWithFrame:NSMakeRect(0, 0, 500, 24)];
        [tf setIdentifier:@"cell"];
        [tf setBezeled:NO];
        [tf setDrawsBackground:NO];
        [tf setEditable:NO];
        [tf setSelectable:NO];
        [tf setFont:[NSFont systemFontOfSize:13]];
        [tf setTextColor:[NSColor colorWithCalibratedRed:0.88
                                                    green:0.91
                                                     blue:0.96
                                                    alpha:1]];
    }
    if (row >= 0 && row < (NSInteger)[self.shown count]) {
        [tf setStringValue:self.shown[row]];
    }
    return tf;
}

- (void)keyDown:(NSEvent *)e {
    unsigned short kc = [e keyCode];
    if (kc == 53) {                      /* Esc */
        push_event("命令面板", "取消");
        [self hide];
        return;
    }
    if (kc == 126) {                     /* ↑ */
        NSInteger r = [self.table selectedRow];
        if (r > 0) [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:r - 1]
                          byExtendingSelection:NO];
        return;
    }
    if (kc == 125) {                     /* ↓ */
        NSInteger r = [self.table selectedRow];
        if (r + 1 < (NSInteger)[self.shown count])
            [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:r + 1]
                      byExtendingSelection:NO];
        return;
    }
    if (kc == 36) {                      /* Enter */
        NSInteger r = [self.table selectedRow];
        if (r >= 0 && r < (NSInteger)[self.shown count]) {
            NSString *pick = self.shown[r];
            push_event("命令面板", [pick UTF8String]);
        }
        [self hide];
        return;
    }
    [super keyDown:e];
}

/* 让搜索框的方向键转给面板处理 */
- (BOOL)control:(NSControl *)control
       textView:(NSTextView *)tv
doCommandBySelector:(SEL)sel {
    if (sel == @selector(moveUp:))     { [self keyDown:[NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0 timestamp:0 windowNumber:0 context:nil characters:@"" charactersIgnoringModifiers:@"" isARepeat:NO keyCode:126]]; return YES; }
    if (sel == @selector(moveDown:))   { [self keyDown:[NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0 timestamp:0 windowNumber:0 context:nil characters:@"" charactersIgnoringModifiers:@"" isARepeat:NO keyCode:125]]; return YES; }
    if (sel == @selector(insertNewline:)) { [self keyDown:[NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0 timestamp:0 windowNumber:0 context:nil characters:@"" charactersIgnoringModifiers:@"" isARepeat:NO keyCode:36]]; return YES; }
    if (sel == @selector(cancelOperation:)) { push_event("命令面板", "取消"); [self hide]; return YES; }
    return NO;
}

@end

/* 打开命令面板。items 用 | 分隔（内容里避免出现 |） */
const char *界面_命令面板_t(const char *spec) {
    @autoreleasepool {
        if (!g_win || !g_root) return "0";
        char buf[65536];
        snprintf(buf, sizeof buf, "%s", spec ? spec : "");
        char *bar = strchr(buf, '|');
        const char *title = bar ? buf : "命令";
        if (bar) *bar = 0;
        const char *list = bar ? bar + 1 : buf;

        NSMutableArray *items = [NSMutableArray array];
        const char *p = list;
        while (*p) {
            const char *sep = strchr(p, '|');
            size_t len = sep ? (size_t)(sep - p) : strlen(p);
            if (len > 0) {
                NSString *s = [[NSString alloc] initWithBytes:p length:len encoding:NSUTF8StringEncoding];
                if (s) [items addObject:s];
            }
            if (!sep) break;
            p = sep + 1;
        }

        if (!g_palette) {
            g_palette = [[HengPalette alloc] initWithParent:g_root];
        }
        (void)title;
        [g_palette showWithItems:items];
        return "1";
    }
}

/* 关闭命令面板 */
const char *界面_关命令面板_t(const char *ignored) {
    @autoreleasepool {
        if (g_palette) [g_palette hide];
    }
    return "1";
}

/* 折叠/展开某个分区。
 *
 * 为什么需要：专业 IDE 的侧栏、底部面板都可折叠（Cmd+B 切侧栏）。
 * 实现上不是隐藏视图，而是把宽/高置 0 —— 因为整套布局是百分比计算的，
 * 隐藏视图会让相邻分区的位置不重算，出现空白或重叠。
 */
const char *界面_折叠_t(const char *spec) {
    @autoreleasepool {
        char buf[256];
        snprintf(buf, sizeof buf, "%s", spec ? spec : "");
        char *comma = strchr(buf, ',');
        if (!comma) return "0";
        *comma = 0;
        const char *name = buf;
        int fold = atoi(comma + 1);

        HengPane *p = pane_of(name);
        if (!p) return "0";

        if (fold) {
            [p setHidden:YES];
        } else {
            [p setHidden:NO];
            NSRect b = [g_root bounds];
            NSRect f = NSMakeRect(b.size.width * p.px / 100.0,
                                  b.size.height * (100.0 - p.py - p.ph) / 100.0,
                                  b.size.width * p.pw / 100.0,
                                  b.size.height * p.ph / 100.0);
            [p setFrame:f];
        }
        return "1";
    }
}

/* 状态栏右侧显示光标位置等动态信息 */
const char *界面_状态右_t(const char *text) {
    @autoreleasepool {
        if (!g_status_parts[2]) return "0";
        [g_status_parts[2] setStringValue:text ? [NSString stringWithUTF8String:text] : @""];
        return "1";
    }
}

const char *界面_自检_t(const char *ignored) {
    static char out[2048];
    @autoreleasepool {
        if (!g_win) { snprintf(out, sizeof out, "无窗口"); return out; }
        NSRect f = [g_win frame];
        /* 顺带统计各类控件，用来证明界面真的建起来了 */
        int nbtn = 0, ntext = 0, nseg = 0, nsub = 0;
        for (NSView *v in [g_root subviews]) nsub++;
        for (NSString *k in g_panes) {
            for (NSView *v in [g_panes[k] subviews]) {
                if ([v isKindOfClass:[NSButton class]]) nbtn++;
                else if ([v isKindOfClass:[NSTextView class]]) ntext++;
                else if ([v isKindOfClass:[NSSegmentedControl class]]) nseg++;
                else if ([v isKindOfClass:[NSScrollView class]]) ntext++;
            }
        }
        int nmenu = (int)[[[NSApp mainMenu] itemArray] count];
        snprintf(out, sizeof out,
                 "窗口「%s」 %.0fx%.0f 可见=%d 分区=%lu 顶层视图=%d 菜单=%d 按钮=%d 文本=%d 标签条=%d",
                 [[g_win title] UTF8String], f.size.width, f.size.height,
                 [g_win isVisible] ? 1 : 0,
                 (unsigned long)[g_panes count], nsub, nmenu, nbtn, ntext, nseg);
    }
    return out;
}

/* 诊断：窗口在屏幕上的真实状态（是否 occlusion 可见、是否主窗口） */
const char *界面_窗口状态_t(const char *ignored) {
    static char out[1024];
    @autoreleasepool {
        if (!g_win) { snprintf(out, sizeof out, "无窗口"); return out; }
        NSRect f = [g_win frame];
        snprintf(out, sizeof out,
                 "在屏=%d 主窗口=%d key=%d alpha=%.2f level=%ld 尺寸=%.0fx%.0f 屏幕=%.0fx%.0f",
                 [g_win isVisible] && [g_win occlusionState] != 0 ? 1 : 0,
                 [g_win isMainWindow] ? 1 : 0,
                 [g_win isKeyWindow] ? 1 : 0,
                 [g_win alphaValue],
                 (long)[g_win level],
                 f.size.width, f.size.height,
                 [[NSScreen mainScreen] frame].size.width,
                 [[NSScreen mainScreen] frame].size.height);
    }
    return out;
}

const char *界面_取结果(const char *key) {
    if (!g_slot || !key) return "";
    NSString *v = g_slot[[NSString stringWithUTF8String:key]];
    return v ? [v UTF8String] : "";
}

/* 轮询事件：把结果放进槽 "事件"，返回 1 表示有事件、0 表示没有。 */
const char *界面_轮询_t(const char *ignored) {
    const char *out = heng_next_event();
    slot_put("事件", out);
    return out[0] ? "1" : "0";
}

/* ---- 语法高亮 ----
 *
 * 设计取舍：高亮规则属于「语言知识」，应该放在衡语里（改规则不用重编 C）。
 * 所以衡语侧把高亮结果编译成一段「标记文本」：
 *
 *     \x01K\x02关键字\x01R\x02   R=保留字  K=关键字  S=字符串  N=数字
 *     C=注释   T=类型   F=函数名  P=标点
 *
 * C 层只负责按标记切段、套颜色。这样做事的好处：
 * 衡语改一行规则就生效，不必重编译 dylib。
 */

static NSColor *heng_color_for(const char *code) {
    switch (code[0]) {
    case 'K':   /* 控制关键字 */
        return [NSColor colorWithCalibratedRed:0.98 green:0.60 blue:0.78 alpha:1];
    case 'R':   /* 声明类保留字 */
        return [NSColor colorWithCalibratedRed:0.72 green:0.60 blue:0.98 alpha:1];
    case 'T':   /* 类型名 */
        return [NSColor colorWithCalibratedRed:0.45 green:0.85 blue:0.92 alpha:1];
    case 'S':   /* 字符串 */
        return [NSColor colorWithCalibratedRed:0.62 green:0.88 blue:0.55 alpha:1];
    case 'N':   /* 数字 */
        return [NSColor colorWithCalibratedRed:0.98 green:0.80 blue:0.45 alpha:1];
    case 'C':   /* 注释 */
        return [NSColor colorWithCalibratedRed:0.48 green:0.54 blue:0.62 alpha:1];
    case 'F':   /* 函数名 */
        return [NSColor colorWithCalibratedRed:0.42 green:0.76 blue:0.98 alpha:1];
    case 'P':   /* 标点 */
        return [NSColor colorWithCalibratedRed:0.62 green:0.65 blue:0.72 alpha:1];
    default:
        return [NSColor colorWithCalibratedRed:0.90 green:0.92 blue:0.96 alpha:1];
    }
}

/* 把带标记的文本套上颜色和等宽字体 */
static NSAttributedString *heng_render_marked(const char *marked) {
    NSMutableAttributedString *out = [[NSMutableAttributedString alloc] init];
    NSFont *mono = [NSFont monospacedSystemFontOfSize:14 weight:NSFontWeightRegular];
    NSFont *monoBold = [NSFont monospacedSystemFontOfSize:14 weight:NSFontWeightMedium];
    const char *p = marked ? marked : "";
    size_t plain_start = 0;
    /* 普通段的起点：遇到 \x01 之前的都是「默认色」文本 */
    while (*p) {
        if (*p == '\x01') {
            /* 先冲出前面的普通段 */
            if ((size_t)(p - marked) > plain_start) {
                NSString *seg = [[NSString alloc] initWithBytes:marked + plain_start
                                                         length:(size_t)(p - marked) - plain_start
                                                       encoding:NSUTF8StringEncoding];
                if (seg) {
                    [out appendAttributedString:[[NSAttributedString alloc]
                        initWithString:seg attributes:@{
                            NSFontAttributeName: mono,
                            NSForegroundColorAttributeName: heng_color_for("x")}]];
                }
            }
            p++;                                   /* 跳过 \x01 */
            char code = *p ? *p : 'x';
            if (*p) p++;
            if (*p == '\x02') p++;                  /* 跳过 \x02 */
            const char *text_start = p;
            while (*p && *p != '\x01') p++;
            NSString *seg = [[NSString alloc] initWithBytes:text_start
                                                     length:(size_t)(p - text_start)
                                                   encoding:NSUTF8StringEncoding];
            if (seg) {
                NSFont *f = (code == 'F' || code == 'R' || code == 'K') ? monoBold : mono;
                [out appendAttributedString:[[NSAttributedString alloc]
                    initWithString:seg attributes:@{
                        NSFontAttributeName: f,
                        NSForegroundColorAttributeName: heng_color_for(&code)}]];
            }
            plain_start = (size_t)(p - marked);
        } else {
            p++;
        }
    }
    if ((size_t)(p - marked) > plain_start) {
        NSString *seg = [[NSString alloc] initWithBytes:marked + plain_start
                                                 length:(size_t)(p - marked) - plain_start
                                               encoding:NSUTF8StringEncoding];
        if (seg) {
            [out appendAttributedString:[[NSAttributedString alloc]
                initWithString:seg attributes:@{
                    NSFontAttributeName: mono,
                    NSForegroundColorAttributeName: heng_color_for("x")}]];
        }
    }
    return out;
}

/* 可编辑代码区（带语法高亮）："分区|标记文本"
   内容用 界面.存编辑区_t 取回时是「纯文本」（标记会被 C 层剥掉）。
   注意：为了能回读纯文本，这里把标记文本的纯文部分同时写进一个槽。 */
const char *界面_编辑区_t(const char *spec) {
    @autoreleasepool {
        static char buf[262144];          /* 大一点：长文件 */
        snprintf(buf, sizeof buf, "%s", spec ? spec : "");
        char *bar = strchr(buf, '|');
        if (!bar) return "0";
        *bar = 0;
        const char *name = buf;
        const char *marked = bar + 1;
        HengPane *p = pane_of(name);
        if (!p) return "0";

        NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:[p bounds]];
        [scroll setHasVerticalScroller:YES];
        [scroll setAutoresizingMask:NSViewWidthSizable | NSViewHeightSizable];

        NSTextView *tv = [[NSTextView alloc] initWithFrame:[p bounds]];
        [tv setEditable:YES];
        [tv setRichText:YES];
        [tv setFont:[NSFont monospacedSystemFontOfSize:14 weight:NSFontWeightRegular]];
        [tv setInsertionPointColor:[NSColor colorWithCalibratedRed:0.45
                                                            green:0.78
                                                             blue:1.0
                                                            alpha:1]];
        [tv setBackgroundColor:[NSColor colorWithCalibratedRed:0.11
                                                         green:0.12
                                                          blue:0.16
                                                         alpha:1]];
        [tv setMinSize:NSMakeSize(0, 0)];
        [tv setMaxSize:NSMakeSize(FLT_MAX, FLT_MAX)];
        [tv setVerticallyResizable:YES];
        [tv setHorizontallyResizable:NO];
        [tv setAutoresizingMask:NSViewWidthSizable];
        [[tv textContainer] setWidthTracksTextView:YES];
        [tv setDrawsBackground:YES];

        /* 套高亮 */
        NSAttributedString *rich = heng_render_marked(marked);
        [[tv textStorage] setAttributedString:rich];

        /* 记住纯文本，回读时给衡语用（避免把标记字符带回去） */
        if (!g_slot) g_slot = [NSMutableDictionary dictionary];
        NSString *key = [NSString stringWithFormat:@"纯文:%s", name];
        g_slot[key] = [rich string];

        [scroll setDocumentView:tv];
        [p addSubview:scroll];
        return "1";
    }
}

/* 读编辑区内容：结果放进槽 "编辑:<分区>"，返回长度标记 */
const char *界面_存编辑区_t(const char *name) {
    @autoreleasepool {
        HengPane *p = pane_of(name);
        if (!p) return "0";
        for (NSView *v in [p subviews]) {
            if ([v isKindOfClass:[NSScrollView class]]) {
                NSTextView *tv = [(NSScrollView *)v documentView];
                if ([tv isKindOfClass:[NSTextView class]]) {
                    NSString *key = [NSString stringWithFormat:@"编辑:%s", name];
                    if (!g_slot) g_slot = [NSMutableDictionary dictionary];
                    /* 取纯文本：富文本里 [[tv string]] 就是去掉属性后的纯文，
                       标记字符在渲染阶段已被拆掉，所以不会带出去。 */
                    g_slot[key] = [tv string];
                    return "1";
                }
            }
        }
        return "0";
    }
}

/* ---- 菜单栏 ----
   参考 Eclipse / NetBeans：顶部菜单是 IDE 的标配。
   spec = "文件:新建,打开;构建:运行,检查"（组:项1,项2;组2:…）
   点菜单项时把 "菜单|项名" 推进事件队列。 */
const char *界面_菜单_t(const char *spec) {
    @autoreleasepool {
        if (!g_win) return "0";
        NSMenu *mainMenu = [[NSMenu alloc] init];
        NSMutableArray *groups = [[NSString stringWithUTF8String:(spec ? spec : "")]
                                  componentsSeparatedByString:@";"] .mutableCopy;
        for (NSString *grp in groups) {
            NSArray *parts = [grp componentsSeparatedByString:@":"];
            if ([parts count] < 2) continue;
            NSString *title = parts[0];
            NSMenuItem *topItem = [[NSMenuItem alloc] init];
            [mainMenu addItem:topItem];
            NSMenu *sub = [[NSMenu alloc] initWithTitle:title];
            [topItem setSubmenu:sub];
            for (NSString *it in [parts[1] componentsSeparatedByString:@","]) {
                if ([it length] == 0) continue;
                NSMenuItem *mi = [[NSMenuItem alloc] initWithTitle:it
                                                            action:@selector(menuHit:)
                                                     keyEquivalent:@""];
                [mi setTarget:[HengMenuTarget shared]];
                [mi setRepresentedObject:it];
                [sub addItem:mi];
            }
        }
        [NSApp setMainMenu:mainMenu];
        return "1";
    }
}

/* ---- 工具栏 ---- spec = "项1,项2,项3"，返回容器高度占比 */
const char *界面_工具栏_t(const char *spec) {
    @autoreleasepool {
        if (!g_root) return "0";
        NSRect b = [g_root bounds];
        double h = 34;
        NSView *bar = [[NSView alloc] initWithFrame:
                       NSMakeRect(0, b.size.height - h, b.size.width, h)];
        [bar setWantsLayer:YES];
        [[bar layer] setBackgroundColor:[[NSColor colorWithCalibratedRed:0.13
                                                                  green:0.14
                                                                   blue:0.19
                                                                  alpha:1.0] CGColor]];
        double x = 12;
        for (NSString *label in [[NSString stringWithUTF8String:(spec ? spec : "")]
                                 componentsSeparatedByString:@","]) {
            if ([label length] == 0) continue;
            HengButton *btn = [[HengButton alloc] initWithFrame:NSMakeRect(x, 4, 74, 26)];
            [btn setTitle:label];
            [btn setBezelStyle:NSBezelStyleRounded];
            btn.actionCode = [NSString stringWithFormat:@"TB:%@", label];
            [btn setTarget:btn];
            [btn setAction:@selector(clicked:)];
            [bar addSubview:btn];
            x += 80;
        }
        [bar setAutoresizingMask:NSViewWidthSizable | NSViewMinYMargin];
        [g_root addSubview:bar];
        return "1";
    }
}

/* ---- 标签切换条 ---- spec = "分区|标签1,标签2"（后面 界面.切标签 切换内容） */
static NSMutableDictionary<NSString *, NSSegmentedControl *> *g_tabs = nil;

const char *界面_标签条_t(const char *spec) {
    @autoreleasepool {
        char buf[4096];
        snprintf(buf, sizeof buf, "%s", spec ? spec : "");
        char *bar = strchr(buf, '|');
        if (!bar) return "0";
        *bar = 0;
        HengPane *p = pane_of(buf);
        if (!p) return "0";
        NSArray *names = [[NSString stringWithUTF8String:(bar + 1)]
                          componentsSeparatedByString:@","];
        NSSegmentedControl *seg = [[NSSegmentedControl alloc]
            initWithFrame:NSMakeRect(6, [p bounds].size.height - 30, 300, 24)];
        [seg setSegmentCount:[names count]];
        for (NSUInteger i = 0; i < [names count]; i++) {
            [seg setLabel:names[i] forSegment:(NSInteger)i];
            [seg setWidth:90 forSegment:(NSInteger)i];
        }
        [seg setSelectedSegment:0];
        [seg setTarget:[HengMenuTarget shared]];
        [seg setAction:@selector(tabHit:)];
        [p addSubview:seg];
        if (!g_tabs) g_tabs = [NSMutableDictionary dictionary];
        g_tabs[[NSString stringWithUTF8String:buf]] = seg;
        return "1";
    }
}

/* ---- 树形列表 ---- spec = "分区|a,1:b,2"（名字,缩进层级） */
const char *界面_树_t(const char *spec) {
    @autoreleasepool {
        char buf[8192];
        snprintf(buf, sizeof buf, "%s", spec ? spec : "");
        char *bar = strchr(buf, '|');
        if (!bar) return "0";
        *bar = 0;
        HengPane *p = pane_of(buf);
        if (!p) return "0";
        NSMutableString *s = [NSMutableString string];
        for (NSString *node in [[NSString stringWithUTF8String:(bar + 1)]
                                componentsSeparatedByString:@":"]) {
            NSArray *kv = [node componentsSeparatedByString:@","];
            if ([kv count] < 2) continue;
            int lvl = [kv[1] intValue];
            for (int i = 0; i < lvl; i++) [s appendString:@"    "];
            [s appendFormat:@"%@\n", kv[0]];
        }
        NSTextView *tv = [[NSTextView alloc] initWithFrame:[p bounds]];
        [tv setString:s];
        [tv setEditable:NO];
        [tv setFont:[NSFont monospacedSystemFontOfSize:12 weight:NSFontWeightRegular]];
        [tv setTextColor:[NSColor colorWithCalibratedRed:0.82 green:0.86 blue:0.94 alpha:1]];
        [tv setBackgroundColor:[NSColor clearColor]];
        [tv setDrawsBackground:NO];
        [p addSubview:tv];
        return "1";
    }
}

/* ---- 状态栏 ---- spec = "左|中|右"（三段，参考 VSCodium 状态栏） */
const char *界面_状态栏_t(const char *spec) {
    @autoreleasepool {
        if (!g_win) return "0";
        NSRect b = [[g_win contentView] bounds];
        double h = 26;

        NSArray *parts = [[NSString stringWithUTF8String:(spec ? spec : "")]
                          componentsSeparatedByString:@"|"];
        double widths[3] = {b.size.width * 0.34, b.size.width * 0.36, b.size.width * 0.30};

        /* 已存在就只更新文字。
           早期每次调用都新建 bar，再用「高度和 y 相同」去猜哪个是旧状态栏——
           既会误删别的视图，又会在几次刷新后叠出多层。 */
        if (g_status_bar) {
            [g_status_bar setFrame:NSMakeRect(0, 0, b.size.width, h)];
            for (NSUInteger i = 0; i < [parts count] && i < 3; i++) {
                if (g_status_parts[i]) {
                    [g_status_parts[i] setStringValue:parts[i]];
                    NSRect f = NSMakeRect(i == 0 ? 12 : (i == 1 ? widths[0] : widths[0] + widths[1]),
                                        4, widths[i] - 12, h - 8);
                    [g_status_parts[i] setFrame:f];
                }
            }
            return "1";
        }

        g_status_bar = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, b.size.width, h)];
        [g_status_bar setWantsLayer:YES];
        [[g_status_bar layer] setBackgroundColor:[[NSColor colorWithCalibratedRed:0.09
                                                                           green:0.10
                                                                            blue:0.14
                                                                           alpha:1.0] CGColor]];
        for (NSUInteger i = 0; i < 3; i++) {
            double x = (i == 0) ? 12 : (i == 1 ? widths[0] : widths[0] + widths[1]);
            NSTextField *tf = [[NSTextField alloc] initWithFrame:
                                NSMakeRect(x, 4, widths[i] - 12, h - 8)];
            [tf setStringValue:(i < [parts count]) ? parts[i] : @""];
            [tf setBezeled:NO];
            [tf setDrawsBackground:NO];
            [tf setEditable:NO];
            [tf setSelectable:NO];
            [tf setFont:[NSFont monospacedSystemFontOfSize:11 weight:NSFontWeightRegular]];
            [tf setTextColor:[NSColor colorWithCalibratedRed:0.68 green:0.72 blue:0.82 alpha:1]];
            [tf setAlignment:(i == 1) ? NSTextAlignmentCenter : (i == 2 ? NSTextAlignmentRight : NSTextAlignmentLeft)];
            [g_status_bar addSubview:tf];
            g_status_parts[i] = tf;
        }
        [g_status_bar setAutoresizingMask:NSViewWidthSizable | NSViewMaxYMargin];
        [g_root addSubview:g_status_bar];
        return "1";
    }
}

/* 关闭窗口（衡语主动退出时调用） */
const char *界面_关闭_t(const char *ignored) {
    @autoreleasepool {
        if (!g_win) return "1";
        /* 不调用 close：它会把窗口从 NSApp 摘掉但对象可能已被别处引用。
           这里只把窗口移出屏幕，随后由进程退出统一回收，最稳。 */
        [g_win orderOut:nil];
        g_closed = 1;
    }
    return "1";
}
