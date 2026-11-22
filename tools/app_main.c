/* 衡语 IDE 的原生启动器。
 *
 * 设计教训（改了三次才对）：
 *   v1 用 shell 脚本 → LaunchServices 不把脚本当应用主体，访达看不到。
 *   v2 用 osascript 开 Terminal 再跑 → 依赖「自动化」权限，
 *      实际效果是「只有终端窗口，图形界面在后面甚至不出现」。
 *   v3（本文件）直接 execv 编译器，stdout 重定向到日志文件，
 *      并切到 Resources 目录。不需要终端、不需要 AppleScript 权限。
 *
 * 为什么用 execv 而不是 fork+exec：进程 PID 不变，
 * LaunchServices 仍认为它就是那个 .app，窗口天然获得前台身份。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <sys/stat.h>

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    char exe[PATH_MAX];
    uint32_t sz = sizeof exe;
    if (_NSGetExecutablePath(exe, &sz) != 0) {
        return 1;
    }

    /* exe = .../Contents/MacOS/launcher → 上溯两级到 Contents */
    char contents[PATH_MAX];
    snprintf(contents, sizeof contents, "%s", exe);
    char *slash = strrchr(contents, '/');
    if (slash) *slash = 0;
    slash = strrchr(contents, '/');
    if (slash) *slash = 0;

    char compiler[PATH_MAX];
    snprintf(compiler, sizeof compiler, "%s/Resources/henger", contents);
    if (access(compiler, X_OK) != 0) {
        return 1;
    }

    char script[PATH_MAX];
    snprintf(script, sizeof script, "%s/Resources/ide.hng", contents);

    /* 日志：GUI 进程没人读 stdout，不重定向的话衡语 说出 时会因 SIGPIPE 死掉，
       表现就是「窗口一闪就没」。 */
    char logpath[PATH_MAX];
    const char *home = getenv("HOME");
    if (!home) home = "/tmp";
    snprintf(logpath, sizeof logpath, "%s/Library/Logs/HengIDE.log", home);
    int logfd = open(logpath, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (logfd >= 0) {
        dup2(logfd, STDOUT_FILENO);
        dup2(logfd, STDERR_FILENO);
        close(logfd);
    }

    /* 切到 Resources：ide.hng 里 导入 外部 "libhengui.dylib" 是相对路径 */
    char resdir[PATH_MAX];
    snprintf(resdir, sizeof resdir, "%s/Resources", contents);
    if (chdir(resdir) != 0) {
        /* 切不过去不致命 */
    }

    char *args[] = {compiler, script, NULL};
    execv(compiler, args);

    return 1;
}
