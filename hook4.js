// hook4.js — 只要 uid / token / signToken，别的都不看
'use strict';

var WANT = ['uid', 'token', 'signToken', 'version', 'timestamp', 'jPushId',
            'account', 'pwd', 'enc', 'd', 'sk', 'ak', 'ck'];
var HEX32 = /^[0-9a-f]{32}$/i;
var seen = {};

function note(k, v) {
    if (seen[k] === v) return;
    seen[k] = v;
    console.log('>>>' + k + '=' + v);
}

function install() {
    Java.perform(function () {
        var HM = Java.use('java.util.HashMap');
        var put = HM.put.overload('java.lang.Object', 'java.lang.Object');
        put.implementation = function (k, v) {
            try {
                var ks = String(k);
                var vs = String(v);
                if (WANT.indexOf(ks) >= 0) note(ks, vs);
                else if (HEX32.test(vs)) note('HEX32_' + ks, vs);
            } catch (e) { }
            return put.call(this, k, v);
        };
        console.log('[dm] HashMap hooked');

        // 只报告与加密/签名相关的类名，方便后续精确定位
        var hits = [];
        Java.enumerateLoadedClasses({
            onMatch: function (n) {
                if (/encrypt|SignUtil|NativeUtil|netRequesthttp/i.test(n)) hits.push(n);
            },
            onComplete: function () {
                console.log('[dm] classes=' + hits.length);
                hits.forEach(function (h) { console.log('[cls] ' + h); });
            }
        });
    });
}

setTimeout(install, 1200);
