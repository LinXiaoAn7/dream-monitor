/*
 * 抓「登录请求里的 pwd 到底是什么」。
 *
 * 思路：不去猜算法，直接在 App 里看它把什么值塞进了参数表。
 *  - HashMap.put：凡是 key 命中 pwd/pass/account/phone/token/sign 的都打印
 *  - com.jingcai...common.encrypt.*：把整个参数表原样 dump 出来
 *  - NativeUtil：看密钥是不是 native 给的（只是参考，不依赖）
 */
'use strict';

const TAG = '[DM]';
const KEYS = /pwd|pass|account|phone|token|sign|mobile|login/i;
let dumped = 0;

function say(s) {
  console.log(TAG + ' ' + s);
}

function safe(fn, dflt) {
  try { return fn(); } catch (e) { return dflt; }
}

/* ---------------------------------------------------------------- Map 入口 */
function hookHashMap() {
  try {
    const HM = Java.use('java.util.HashMap');
    HM.put.overload('java.lang.Object', 'java.lang.Object').implementation = function (k, v) {
      const ks = safe(() => String(k), '?');
      if (KEYS.test(ks)) {
        say('put ' + ks + ' = ' + safe(() => String(v), '?'));
      }
      return this.put(k, v);
    };
    say('HashMap.put hooked');
  } catch (e) {
    say('HashMap.put hook 失败: ' + e);
  }
}

/* ---------------------------------------------------------------- 加密类 */
function hookEncryptClasses() {
  const names = safe(() => Java.enumerateLoadedClassesSync(), [])
    .filter((n) => n.indexOf('com.jingcai.apps.qualitydev') >= 0 &&
                   /encrypt|sign|crypt|security|util\.Native/i.test(n));

  say('候选类 ' + names.length + ' 个');

  names.slice(0, 40).forEach((name) => {
    let C;
    try { C = Java.use(name); } catch (e) { return; }

    const methods = safe(() => C.class.getDeclaredMethods(), []);
    methods.forEach((m) => {
      const mname = safe(() => m.getName(), '');
      const params = safe(() => m.getParameterTypes(), []);
      const takesMap = params.some((p) => String(p).indexOf('Map') >= 0);
      const takesStr = params.some((p) => String(p).indexOf('String') >= 0);
      if (!(takesMap || takesStr)) return;
      if (!/^[a-z]$/.test(mname) && !/encrypt|sign|encode|decrypt|key/i.test(mname)) return;

      const sig = params.map((p) => p.getSimpleName()).join(',');
      let impl;
      try {
        impl = C[mname].overload(sig);
      } catch (e) {
        return;
      }
      impl.implementation = function () {
        let dump = '';
        for (let i = 0; i < arguments.length; i++) {
          const a = arguments[i];
          const t = safe(() => a.$className, '');
          if (t && t.indexOf('Map') >= 0) {
            dump += ' map=' + safe(() => JSON.stringify(Object.entries(a).slice(0, 40)), '{}');
          } else if (t === 'java.lang.String') {
            dump += ' str=' + safe(() => String(a), '?');
          }
        }
        const ret = impl.apply(this, arguments);
        say(name + '#' + mname + '(' + sig + ')' + dump +
            ' -> ' + safe(() => (ret === null ? 'null' : String(ret)), '?'));
        return ret;
      };
    });
  });
}

/* ---------------------------------------------------------------- 网络层兜底 */
function hookOkhttp() {
  try {
    const B = Java.use('okhttp3.Request$Builder');
    B.method.overload('java.lang.String', 'okhttp3.RequestBody').implementation = function (m, body) {
      const b = safe(() => body.toString(), '?');
      say('REQUEST ' + m + ' body=' + b.substring(0, 400));
      return this.method(m, body);
    };
    say('okhttp3.Request$Builder.method hooked');
  } catch (e) {
    say('okhttp hook 失败（类名可能被混淆）: ' + e);
  }
}

Java.perform(function () {
  say('frida 注入成功，开始挂钩');
  hookHashMap();
  hookOkhttp();
  hookEncryptClasses();
  say('挂钩完成');
});
