// hook3.js — 从内存里抄 token / uid
// 用法: frida -U -f com.jingcai.apps.qualitydev -l hook3.js
'use strict';

var PKG = 'com.jingcai.apps.qualitydev';
var seen = {};

function log(s) { console.log('[dm] ' + s); }
function note(k, v) {
    if (seen[k] === v) return;
    seen[k] = v;
    log('>>> ' + k + ' = ' + v);
}

// ---------- native: 反调试 ----------
try {
    var ptrace = Module.findExportByName(null, 'ptrace');
    if (ptrace) {
        Interceptor.replace(ptrace, new NativeCallback(function (req, pid, a, b) {
            return 0;
        }, 'long', ['int', 'int', 'pointer', 'pointer']));
        log('ptrace disabled');
    }
    var read = Module.findExportByName(null, 'read');
    if (read) {
        Interceptor.attach(read, {
            onEnter: function (args) { this.fd = args[0].toInt32(); this.buf = args[1]; this.cnt = args[2].toInt32(); },
            onLeave: function (ret) {
                try {
                    var n = ret.toInt32();
                    if (n <= 0 || n > 4096) return;
                    var s = Memory.readUtf8String(this.buf, n);
                    if (s && s.indexOf('TracerPid') >= 0) {
                        var fixed = s.replace(/TracerPid:\s*\d+/, 'TracerPid:\t0');
                        if (fixed.length === s.length) Memory.writeUtf8String(this.buf, fixed);
                    }
                } catch (e) {}
            }
        });
        log('TracerPid spoofed');
    }
} catch (e) { log('native hook fail: ' + e); }

// ---------- java ----------
function hookEncrypt() {
    var names = [
        'com.jingcai.apps.qualitydev.common.encrypt.i',
        'com.jingcai.apps.qualitydev.common.encrypt.e',
        'com.jingcai.apps.qualitydev.common.encrypt.a'
    ];
    names.forEach(function (n) {
        try {
            var C = Java.use(n);
            var ms = C.class.getDeclaredMethods();
            ms.forEach(function (m) {
                var mn = m.getName();
                if (m.getParameterTypes().length !== 1) return;
                var pt = m.getParameterTypes()[0].getName();
                if (pt.indexOf('Map') < 0 && pt.indexOf('JSON') < 0 && pt !== 'java.lang.String') return;
                try {
                    C[mn].overload(pt).implementation = function (arg) {
                        var dump = null;
                        try { dump = JSON.stringify(arg); } catch (e) { dump = String(arg); }
                        log('[' + n + '.' + mn + '] ' + dump);
                        return this[mn](arg);
                    };
                    log('hooked ' + n + '.' + mn + '(' + pt + ')');
                } catch (e) {}
            });
        } catch (e) { log('no class ' + n); }
    });
}

function hookHashmap() {
    try {
        var HM = Java.use('java.util.HashMap');
        var put = HM.put.overload('java.lang.Object', 'java.lang.Object');
        put.implementation = function (k, v) {
            try {
                var ks = String(k), vs = String(v);
                if (/token|uid|sign|account|pwd|secret|authorization/i.test(ks)) note(ks, vs);
                else if (/^[0-9a-f]{32}$/i.test(vs)) note('hash32:' + ks, vs);
            } catch (e) {}
            return put.call(this, k, v);
        };
        log('HashMap.put hooked');
    } catch (e) { log('hashmap fail ' + e); }
}

function hookOkhttp() {
    try {
        var RB = Java.use('okhttp3.Request$Builder');
        ['header', 'addHeader'].forEach(function (m) {
            try {
                RB[m].overload('java.lang.String', 'java.lang.String').implementation = function (k, v) {
                    try {
                        if (/token|uid|sign|auth|cookie/i.test(k)) note('hdr:' + k, v);
                    } catch (e) {}
                    return this[m](k, v);
                };
                log('okhttp ' + m + ' hooked');
            } catch (e) {}
        });
    } catch (e) { log('okhttp fail ' + e); }
}

Java.perform(function () {
    log('attached to ' + PKG);
    hookEncrypt();
    hookHashmap();
    hookOkhttp();

    // 兜底：主动读 SharedPreferences 里的登录态
    setTimeout(function () {
        Java.perform(function () {
            try {
                var ctx = Java.use('android.app.ActivityThread').currentApplication().getApplicationContext();
                var prefs = ctx.getSharedPreferences('authStatus_' + PKG, 0);
                var all = prefs.getAll();
                var it = all.keySet().iterator();
                while (it.hasNext()) {
                    var k = String(it.next());
                    log('pref ' + k + ' = ' + String(all.get(k)).substring(0, 120));
                }
            } catch (e) { log('prefs fail ' + e); }
        });
    }, 4000);
});
