/*
 * hook.js —— 从到梦空间 App 内存里直接打印明文参数表（含 uid / token）
 *
 * 前置：一台 root 过的安卓设备或自带 root 的模拟器（MuMu / 雷电）
 *   1. 装 frida-server（对应 abi）到 /data/local/tmp，chmod 755，root 下运行
 *   2. 电脑：pip install frida-tools
 *   3. 电脑：frida -U -f com.jingcai.apps.qualitydev -l hook.js --no-pause
 *   4. 在 App 里点一下「活动」页，终端会刷出 [params] {... uid, token ...}
 *
 * 只需抄下 uid 和 token 两行，填进 config.yaml 即可。之后可长期使用。
 */

Java.perform(function () {
    var seen = {};

    function dump(tag, map) {
        try {
            var s = String(map);
            // 只关心带 uid/token 的参数表，过滤噪音
            if (s.indexOf("uid") === -1 && s.indexOf("token") === -1) return;
            var key = s.length + "|" + s.slice(0, 64);
            if (seen[key]) return;
            seen[key] = true;
            console.log("\n[" + tag + "] " + s);

            // 顺手把关键字段单独拎出来，方便复制
            var m = s.replace(/^\{|\}$/g, "").split(", ");
            var kv = {};
            m.forEach(function (p) {
                var i = p.indexOf("=");
                if (i > 0) kv[p.slice(0, i).trim()] = p.slice(i + 1).trim();
            });
            if (kv.uid && kv.token) {
                console.log(">>> uid   = " + kv.uid);
                console.log(">>> token = " + kv.token);
                if (kv.jPushId) console.log(">>> jPushId = " + kv.jPushId);
            }
        } catch (e) {
            console.log("[" + tag + "] dump error: " + e);
        }
    }

    // 主目标：BaseRequest.getHashMap() —— 文章逆向确认的参数注入点
    try {
        var BaseRequest = Java.use("com.jingcai.apps.qualitydev.network.netRequesthttp.BaseRequest");
        BaseRequest.getHashMap.implementation = function () {
            var m = this.getHashMap();
            dump("BaseRequest.getHashMap", m);
            return m;
        };
        console.log("[+] hooked BaseRequest.getHashMap");
    } catch (e) {
        console.log("[-] BaseRequest.getHashMap 未找到: " + e);
    }

    // 兜底 1：部分版本参数在 signToken 生成处附近拼装
    try {
        var Sign = Java.use("com.jingcai.apps.qualitydev.network.netRequesthttp.SignUtil");
        if (Sign.getSign) {
            Sign.getSign.overloads.forEach(function (ov) {
                ov.implementation = function () {
                    var r = ov.apply(this, arguments);
                    try { dump("SignUtil.getSign args", arguments[0]); } catch (e) {}
                    return r;
                };
            });
            console.log("[+] hooked SignUtil.getSign");
        }
    } catch (e) {
        console.log("[-] SignUtil 未找到（可忽略）: " + e);
    }

    // 兜底 2：直接盯 HashMap.put，抓 uid/token 落表那一刻
    try {
        var HashMap = Java.use("java.util.HashMap");
        HashMap.put.implementation = function (k, v) {
            try {
                var ks = String(k);
                if (ks === "uid" || ks === "token" || ks === "jPushId") {
                    console.log(">>> [HashMap.put] " + ks + " = " + v);
                }
            } catch (e) {}
            return this.put(k, v);
        };
        console.log("[+] hooked HashMap.put");
    } catch (e) {
        console.log("[-] HashMap.put hook 失败: " + e);
    }

    console.log("\n[*] 已就绪，现在去 App 里点开「活动」页面。\n");
});
