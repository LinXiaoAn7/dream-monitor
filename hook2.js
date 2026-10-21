/*
 * 到梦空间 token 抓取 v2
 *   frida -U -f com.jingcai.apps.qualitydev -l hook2.js -o hook.log --no-pause
 *
 * 设计思路：
 *   这个 App 是加固壳，dex 在运行时才解密，静态看到的类名全是壳的。
 *   所以第一步先在内存里把「真实类名」枚举出来，第二步才挂精准的 hook。
 *   同时挂一些不依赖类名的通用网络层 hook 兜底。
 */

var TAG = "[dm]";
var MAX_CLASSES = 400;   // 枚举输出上限，避免刷屏

Java.perform(function () {
    console.log(TAG + " 注入成功, Android " + (Java.androidVersion || "?"));

    // ---------- 第一步：枚举运行时真实类名 ----------
    var KEY = /(http|net|api|sign|encrypt|decrypt|aes|rsa|token|login|request|header|param)/i;
    var seen = 0;
    console.log(TAG + " === 开始枚举 com.jingcai 下的类 ===");
    Java.enumerateLoadedClassesSync().forEach(function (n) {
        if (seen >= MAX_CLASSES) return;
        if (n.indexOf("com.jingcai") !== 0 && n.indexOf("com.jingcai") < 0) return;
        if (!KEY.test(n)) return;
        seen++;
        console.log(TAG + " CLASS " + n);
    });
    console.log(TAG + " === 枚举结束，命中 " + seen + " 个 ===");

    // ---------- 第二步：通用网络层 hook（不依赖业务类名）----------
    function hit(k, v) {
        if (!v) return false;
        var s = String(v);
        if (/^[0-9a-f]{32}$/i.test(s)) return true;              // 32 位 hex，uid/token 的典型形态
        var kl = String(k).toLowerCase();
        return kl.indexOf("token") >= 0 || kl.indexOf("uid") >= 0 ||
               kl.indexOf("authorization") >= 0 || kl.indexOf("cookie") >= 0;
    }

    // OkHttp3
    try {
        var RB = Java.use("okhttp3.Request$Builder");
        ["header", "addHeader"].forEach(function (m) {
            var f = RB[m];
            f.implementation = function (k, v) {
                if (hit(k, v)) console.log(TAG + " [OkHttp." + m + "] " + k + " = " + v);
                return f.call(this, k, v);
            };
        });
        console.log(TAG + " [+] okhttp3 已挂");
    } catch (e) { console.log(TAG + " [-] okhttp3 无: " + e); }

    // HttpURLConnection
    try {
        var UC = Java.use("java.net.URLConnection");
        var sp = UC.setRequestProperty;
        sp.implementation = function (k, v) {
            if (hit(k, v)) console.log(TAG + " [URLConn] " + k + " = " + v);
            return sp.call(this, k, v);
        };
        console.log(TAG + " [+] URLConnection 已挂");
    } catch (e) { console.log(TAG + " [-] URLConnection: " + e); }

    // 请求体：HttpURLConnection 的 outputStream 里就是 `d=密文`，但明文 JSON 在这里
    try {
        var HM = Java.use("java.util.HashMap");
        var put = HM.put;
        put.implementation = function (k, v) {
            try {
                if (hit(k, v)) console.log(TAG + " [HashMap.put] " + k + " = " + v);
            } catch (e) {}
            return put.call(this, k, v);
        };
        console.log(TAG + " [+] HashMap.put 已挂");
    } catch (e) {}

    // ---------- 第三步：把响应体也打出来，方便判断登录是否成功 ----------
    try {
        var BR = Java.use("java.io.BufferedReader");
        var rd = BR.readLine;
        rd.implementation = function () {
            var line = rd.call(this);
            try {
                if (line && (line.indexOf("\"code\"") >= 0 || line.indexOf("token") >= 0)) {
                    console.log(TAG + " [resp] " + String(line).substring(0, 300));
                }
            } catch (e) {}
            return line;
        };
        console.log(TAG + " [+] BufferedReader 已挂");
    } catch (e) {}

    console.log(TAG + " === 现在去手机上点开「活动」列表页 ===");
});
