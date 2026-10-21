/*
 * 附加式挂钩：App 已经跑起来之后再注入，这时业务类都已加载（加固壳解完密）。
 *
 * 目标：抓到登录请求里 pwd 字段的真实值。
 *  1) 任何 com.jingcai.* 下名字带 encrypt/sign 的类，凡是吃 Map 的方法，原样 dump 参数表
 *  2) 网络层 BaseRequest.getHashMap —— 直接看发出去之前那张表
 *  3) HashMap.put 严格匹配 key，只认 pwd/password/account/phone/token 这几个确切的键
 */
'use strict';

const TAG = '[DM]';
const EXACT = new Set(['pwd', 'password', 'passwd', 'account', 'phone', 'mobile',
                       'token', 'signToken', 'uid', 'loginName', 'userName']);
let seen = 0;

function say(s) { console.log(TAG + ' ' + s); }
function safe(fn, d) { try { return fn(); } catch (e) { return d; } }

function dumpMap(m) {
  try {
    const keys = m.keySet().toArray();
    const out = [];
    for (let i = 0; i < keys.length; i++) {
      const k = String(keys[i]);
      const v = String(m.get(keys[i]));
      out.push(k + '=' + (v.length > 160 ? v.substring(0, 160) + '…' : v));
    }
    return '{' + out.join(', ') + '}';
  } catch (e) {
    return '<dump失败:' + e + '>';
  }
}

/* 1. 严格 HashMap.put */
function hookPut() {
  const HM = Java.use('java.util.HashMap');
  HM.put.overload('java.lang.Object', 'java.lang.Object').implementation = function (k, v) {
    const ks = safe(() => String(k), '');
    if (EXACT.has(ks)) {
      say('PUT ' + ks + ' = ' + safe(() => String(v), '?'));
    }
    return this.put(k, v);
  };
  say('HashMap.put 已挂（严格匹配）');
}

/* 2. 业务加密类 */
function hookEncrypt() {
  const all = safe(() => Java.enumerateLoadedClassesSync(), []);
  const cands = all.filter((n) =>
    n.indexOf('com.jingcai') >= 0 && /encrypt|Encrypt|sign|Sign|crypt/.test(n));
  say('加密/签名相关类 ' + cands.length + ' 个: ' + cands.slice(0, 12).join(' , '));

  let hooked = 0;
  cands.forEach((name) => {
    let C;
    try { C = Java.use(name); } catch (e) { return; }
    const ms = safe(() => C.class.getDeclaredMethods(), []);
    ms.forEach((m) => {
      const mn = safe(() => m.getName(), '');
      const ps = safe(() => m.getParameterTypes(), []);
      const takesMap = ps.some((p) => String(p).indexOf('Map') >= 0);
      if (!takesMap) return;
      const sig = ps.map((p) => p.getSimpleName()).join(',');
      let impl;
      try { impl = C[mn].overload(sig); } catch (e) { return; }
      impl.implementation = function () {
        const before = safe(() => dumpMap(arguments[0]), '');
        const ret = impl.apply(this, arguments);
        say('CALL ' + name + '#' + mn + '(' + sig + ') 入参=' + before +
            ' 返回=' + safe(() => (ret === null ? 'null' : String(ret)), '?').substring(0, 300));
        return ret;
      };
      hooked++;
    });
  });
  say('已挂 ' + hooked + ' 个 Map 方法');
}

/* 3. 网络层参数表 */
function hookNet() {
  const all = safe(() => Java.enumerateLoadedClassesSync(), []);
  const nets = all.filter((n) => n.indexOf('com.jingcai') >= 0 && /network|http|request/i.test(n));
  say('网络相关类 ' + nets.length + ' 个');
  nets.forEach((name) => {
    let C;
    try { C = Java.use(name); } catch (e) { return; }
    const ms = safe(() => C.class.getDeclaredMethods(), []);
    ms.forEach((m) => {
      const mn = safe(() => m.getName(), '');
      if (!/getHashMap|getParam|buildParam|getParams/i.test(mn)) return;
      const ps = safe(() => m.getParameterTypes(), []);
      const sig = ps.map((p) => p.getSimpleName()).join(',');
      let impl;
      try { impl = C[mn].overload(sig); } catch (e) { return; }
      impl.implementation = function () {
        const ret = impl.apply(this, arguments);
        say('NET ' + name + '#' + mn + ' -> ' + safe(() => dumpMap(ret), ''));
        return ret;
      };
    });
  });
}

Java.perform(function () {
  say('注入成功（附加模式）');
  hookPut();
  hookEncrypt();
  hookNet();
  say('准备就绪，请在手机上退出登录再重新登录');
});
