# 窗口验证工具

## 为什么需要它

之前确认 GUI「是否存在」用的是进程自己的日志：
```
窗口「衡语 IDE」 1268x741 可见=1 分区=6
```
这是**进程的主观判断**——窗口对象建了、`isVisible` 返回 YES，但不代表它在系统窗口服务器里真实呈现、也不代表用户能看到。

## 正确的验证方式

问系统窗口服务器（WindowServer）要「当前屏幕上真实存在的窗口列表」：

```sh
clang -O2 -o /tmp/wlist tools/window_probe.m -framework Cocoa
/tmp/wlist | grep 衡语
```

输出示例：
```
[0] owner=衡语 IDE name=  size=1268x741
```

`owner` 是关键：它会显示为 `hengvm`（裸二进制）或 `衡语 IDE`（从 .app 启动），
后者才说明进程拿到了正确的应用身份。

## 这次查出的两个真实问题

1. **裸二进制启动** → owner=hengvm，窗口没有应用身份，在各窗口后面
2. **execv 保持 PID 且 cwd 在 Resources** → 从 .app 启动后 owner=衡语 IDE，窗口在前台
