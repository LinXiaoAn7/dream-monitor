/*
 * 到梦空间 模拟器运行 + 明文参数抓取
 * 用法：
 *   frida -U -f com.jingcai.apps.qualitydev -l bypass.js --no-pause
 *
 * 目标（按优先级）：
 *   1. 让 App 别在启动时自杀（拦退出路径 + 打印谁在报警）
 *   2. 挂住签名/加密函数，把【明文参数表】原样打印出来
 *      -> 一次拿到 pwd 的真实构造方式
 *   3. 顺手把返回体的 token 也捞出来
 */

var TAG = "[dm]";

/* ============ 一、native 层：反调试（照搬参考项目） ============ */
function hookNativeAntiDebug() {
    try {
        Interceptor.attach(Module.findExportByName(null, "ptrace"), {
            onEnter: function (args) { args[0] = ptr(-1); },
            onLeave: function (retval) { retval.replace(0); }
        });
        console.log(TAG + " [+] ptrace 已拦");
    } catch (e) { console.log(TAG + " [-] ptrace: " + e.message); }

    try {
        Interceptor.attach(Module.findExportByName(null, "read"), {
            onEnter: function (args) {
                this.buf = args[1];
                this.cnt = args[2].toInt32();
            },
            onLeave: function (retval) {
                if (retval.toInt32() > 0) {
                    try {
                        var s = Memory.readUtf8String(this.buf, Math.min(retval.toInt32(), 256));
                        if (s && s.indexOf("TracerPid:") >= 0) {
                            Memory.writeUtf8String(this.buf,
                                s.replace(/TracerPid:\s*\d+/, "TracerPid:\t0"));
                        }
                    } catch (e) {}
                }
            }
        });
        console.log(TAG + " [+] TracerPid 已伪造");
    } catch (e) { console.log(TAG + " [-] read: " + e.message); }

    // 等 libnesec.so 加载后，掐掉它的反调试线程
    try {
        Interceptor.attach(Module.findExportByName(null, "android_dlopen_ext"), {
            onEnter: function (args) {
                try { this.p = args[0].readCString(); } catch (e) { this.p = ""; }
            },
            onLeave: function () {
                if (this.p && this.p.indexOf("libnesec.so") >= 0) {
                    var base = Module.findBaseAddress("libnesec.so");
                    console.log(TAG + " [i] libnesec.so @ " + base);
                    try {
                        Interceptor.attach(Module.findExportByName("libc.so", "pthread_create"), {
                            onEnter: function (a) {
                                var fn = a[2];
                                if (!base) return;
                                var off = fn.sub(base).toString(16);
                                // 参考项目实测到的反调试线程特征
                                if (off === "a47e4") {
                                    console.log(TAG + " [!] 反调试线程 " + off + " 已掐断");
                                    Interceptor.replace(fn, new NativeCallback(function () {
                                        return ptr(0);
                                    }, "pointer", []));
                                }
                            }
                        });
                        console.log(TAG + " [+] pthread_create 已挂");
                    } catch (e) { console.log(TAG + " [-] pthread_create: " + e.message); }
                }
            }
        });
    } catch (e) { console.log(TAG + " [-] dlopen: " + e.message); }
}

hookNativeAntiDebug();

/* ============ 二、Java 层：别让它自杀 ============ */
Java.perform(function () {
    console.log(TAG + " Java 注入, Android " + (Java.androidVersion || "?"));

    // 2.1 拦住所有退出路径 —— 检测失败时的默认动作就是杀进程
    function blockExit(cls, method, sig) {
        try {
            var C = Java.use(cls);
            C[method].overloads.forEach(function (ov) {
                ov.implementation = function () {
                    var args = [];
                    for (var i = 0; i < arguments.length; i++) args.push(String(arguments[i]));
                    console.log(TAG + " [BLOCKED] " + cls.split(".").pop() + "." + method +
                                "(" + args.join(", ") + ")");
                    // 不调用原函数
                };
            });
            console.log(TAG + " [+] 拦住 " + cls + "." + method);
        } catch (e) { }
    }
    blockExit("java.lang.System", "exit");
    blockExit("java.lang.Runtime", "halt");
    blockExit("android.os.Process", "killProcess");

    // 2.2 谁在弹 Toast？把调用栈打出来，锁定检测点
    try {
        var Toast = Java.use("android.widget.Toast");
        var show = Toast.show;
        show.implementation = function () {
            try {
                var txt = "";
                var v = this.getView();
                if (v) { try { txt = v.getText().toString(); } catch (e) {} }
                console.log(TAG + " [Toast] " + txt);
                if (txt.indexOf("Root") >= 0 || txt.indexOf("root") >= 0 ||
                    txt.indexOf("模拟器") >= 0 || txt.indexOf("环境") >= 0) {
                    var st = Java.use("android.util.Log").getStackTraceString(
                        Java.use("java.lang.Throwable").$new());
                    console.log(TAG + " [Toast-STACK] " + st.split("\n").slice(0, 14).join("\n"));
                }
            } catch (e) {}
            return show.call(this);
        };
        console.log(TAG + " [+] Toast 已挂");
    } catch (e) { console.log(TAG + " [-] Toast: " + e.message); }

    // 2.3 常见的 su 路径探测，一律说"没有"
    try {
        var F = Java.use("java.io.File");
        var ex = F.exists;
        ex.implementation = function () {
            var p = "";
            try { p = this.getAbsolutePath(); } catch (e) {}
            if (/\/su$|\/xbin\/su|\/sbin\/su|magisk|superuser|superSU/i.test(p)) {
                console.log(TAG + " [File.exists->false] " + p);
                return false;
            }
            return ex.call(this);
        };
        console.log(TAG + " [+] File.exists 已挂");
    } catch (e) {}

    // 2.4 谁在执行 su
    try {
        var RT = Java.use("java.lang.Runtime");
        var run = RT.exec.overload("java.lang.String");
        run.implementation = function (cmd) {
            if (/su|magisk|which/i.test(String(cmd))) {
                console.log(TAG + " [Runtime.exec] " + cmd);
            }
            return run.call(this, cmd);
        };
        console.log(TAG + " [+] Runtime.exec 已挂");
    } catch (e) {}

    /* ============ 三、核心：把明文参数表挖出来 ============ */
    function dumpMap(map) {
        var out = [];
        try {
            var it = map.entrySet().iterator();
            while (it.hasNext()) {
                var e = it.next();
                out.push(e.getKey() + "=" + e.getValue());
            }
        } catch (e) {
            try { out.push(String(map)); } catch (e2) {}
        }
        return out;
    }

    // 签名函数：i.d(Map) —— 这里收到的就是完整明文参数
    try {
        var I = Java.use("com.jingcai.apps.qualitydev.common.encrypt.i");
        I.d.overloads.forEach(function (ov) {
            ov.implementation = function (map) {
                console.log(TAG + " ===== 签名入参（明文！）=====");
                dumpMap(map).forEach(function (s) { console.log(TAG + "   " + s); });
                var r = ov.call(this, map);
                console.log(TAG + "   签名 = " + r);
                console.log(TAG + " ============================");
                return r;
            };
        });
        console.log(TAG + " [+] encrypt.i.d 已挂");
    } catch (e) { console.log(TAG + " [-] encrypt.i: " + e.message); }

    // 加密函数：e.a(Map) —— 再兜一层
    try {
        var E = Java.use("com.jingcai.apps.qualitydev.common.encrypt.e");
        E.a.overloads.forEach(function (ov) {
            ov.implementation = function (map) {
                console.log(TAG + " ----- 加密入参（明文）-----");
                dumpMap(map).forEach(function (s) { console.log(TAG + "   " + s); });
                var r = ov.call(this, map);
                console.log(TAG + " ----- 加密完成 -----");
                return r;
            };
        });
        console.log(TAG + " [+] encrypt.e.a 已挂");
    } catch (e) { console.log(TAG + " [-] encrypt.e: " + e.message); }

    // 兜底：任何返回体里带 token 的都打出来
    try {
        var BR = Java.use("java.io.BufferedReader");
        var rl = BR.readLine;
        rl.implementation = function () {
            var line = rl.call(this);
            try {
                if (line && (line.indexOf("\"token\"") >= 0 || line.indexOf("\"code\"") >= 0)) {
                    console.log(TAG + " [resp] " + String(line).substring(0, 400));
                }
            } catch (e) {}
            return line;
        };
        console.log(TAG + " [+] BufferedReader 已挂");
    } catch (e) {}

    console.log(TAG + " === 就绪：App 起来后在登录页输入账号密码并登录 ===");
});
