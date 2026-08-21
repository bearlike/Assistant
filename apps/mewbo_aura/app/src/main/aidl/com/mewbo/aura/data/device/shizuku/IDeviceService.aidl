package com.mewbo.aura.data.device.shizuku;

/**
 * The device-control surface, executed inside a Shizuku UserService at shell
 * UID (2000). Every method here runs in THAT process, not in the app's.
 *
 * `destroy()` at transaction 16777114 is Shizuku's own reserved code — the
 * server calls it on unbind, and without it the service process outlives the
 * app. Verified against RikkaApps/Shizuku-API's demo IUserService.aidl.
 */
interface IDeviceService {

    void destroy() = 16777114; // reserved by the Shizuku server

    /** `wm size` / `wm density` ground truth, as "widthxheight:density". */
    String displayInfo() = 1;

    /** Pruned, indexed element list of the current screen, as JSON. */
    String elements() = 2;

    // Transaction 3 is RETIRED, not free. It was `screenshot(int, int)`,
    // returning base64 or "" — a failure that could not say why. Never reuse
    // the number: a bound service in an already-installed APK still answers on
    // it, so a new method taking 3 mis-dispatches SILENTLY at runtime instead
    // of failing to build. Its replacement is `captureScreen` below.

    /** Inject a tap at device coordinates. */
    boolean tap(int x, int y) = 4;

    /** Inject a swipe between two device coordinates. */
    boolean swipe(int x1, int y1, int x2, int y2, int durationMs) = 5;

    /** Type text into the focused field. */
    boolean typeText(String text) = 6;

    /** Press a global key: back, home, recents, enter. */
    boolean pressKey(String key) = 7;

    /** Launch an app by package name. */
    boolean launch(String packageName) = 8;

    /** Run a shell command, returning combined stdout+stderr. */
    String shell(String command, int timeoutMs) = 9;

    /**
     * Screenshot as a JSON envelope — `{"image_base64","image_media_type"}` on
     * success, `{"error":{"code","message"}}` naming the cause and the cure on
     * failure. A protected window (`FLAG_SECURE`) is an ordinary screen here,
     * not a corner case, so the failure has to be readable.
     */
    String captureScreen(int maxWidth, int quality) = 10;
}
