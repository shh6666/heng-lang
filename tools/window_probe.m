#import <Cocoa/Cocoa.h>
int main(){
    @autoreleasepool {
        CFArrayRef wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID);
        NSArray *arr = (__bridge_transfer NSArray *)wins;
        printf("屏幕上窗口总数: %lu\n", (unsigned long)[arr count]);
        for (NSDictionary *w in arr) {
            NSString *owner = w[(id)kCGWindowOwnerName];
            NSString *name = w[(id)kCGWindowName];
            NSDictionary *b = w[(id)kCGWindowBounds];
            int layer = [w[(id)kCGWindowLayer] intValue];
            printf("  [%d] owner=%s name=%s  size=%.0fx%.0f\n",
                   layer, owner ? [owner UTF8String] : "?",
                   name ? [name UTF8String] : "",
                   [b[@"Width"] doubleValue], [b[@"Height"] doubleValue]);
        }
    }
    return 0;
}
