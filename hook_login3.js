/*
 * 启动即注入版（spawn）：业务类要等加固壳解完密才存在，所以挂钩分两步——
 *   1. 立即挂 HashMap.put（bootclasspath，随时可用）
 *   2. 每秒尝试一次，等 com.jingcai 的加密/网络类出现后挂上
 *
 * 目标：抓到登录请求里 pwd 的真实值，从而拿到「账号+密码换 token」的钥匙。
 */
'use strict';

const TAG = '[DM]';
const EXACT = new Set(['pwd', 'password', 'passwd', 'account', 'phone', 'mobile',
                       'token', 'signToken', 'uid', 'loginName', 'userName']);
let armed = false;
let tries = 0;
let hitCount = 0;

function say(s) { console.log(TAG + ' ' + s); }
function safe(fn, d) { try { return fn(); } catch (e) { return d; } }

function dumpMap(m) {
  try {
    const keys = m.keySet().toArray();
    const out = [];
    for (let i = 0; i < keys.length; i++) {
      const k = String(keys[i]);
      let v = String(m.get(keys[i]));
      if (v.length > 200) v = v.substring(0, 200) + '…(' + v.length + '字)';
      out.push(k + '=' + v);
    }
    return '{' + out.join(', ') + '}';
  } catch (e) {
    return '<dump失败:' + e + '>';
  }
}

function hookPut() {
  const HM = Java.use('java.util.HashMap');
  HM.put.overload('java.lang.Object', 'java.lang.Object').implementation = function (k, v) {
    const ks = safe(() => String(k), '');
    if (EXACT.has(ks)) {
      hitCount++;
      say('PUT ' + ks + ' = ' + safe(() => String(v), '?'));
    }
    return this.put(k, v);
  };
  say('HashMap.put 已挂');
}

function arm() {
  if (armed) return true;
  const all = safe(() => Java.enumerateLoadedClassesSync(), []);
  const jc = all.filter((n) => n.indexOf('com.jingcai') >= 0);
  if (jc.length < 50) return false;      // 业务 dex 还没解出来

  const enc = jc.filter((n) => /encrypt|Encrypt|sign|Sign|crypt/.test(n));
  const net = jc.filter((n) => /network|http|request/i.test(n));
  say('业务类 ' + jc.length + ' 个｜加密 ' + enc.length + '｜网络 ' + net.length);
  say('加密类样本: ' + enc.slice(0, 10).join(' , '));

  let n = 0;
  enc.forEach((name) => {
    let C; try { C = Java.use(name); } catch (e) { return; }
    safe(() => C.class.getDeclaredMethods(), []).forEach((m) => {
      const mn = safe(() => m.getName(), '');
      const ps = safe(() => m.getParameterTypes(), []);
      if (!ps.some((p) => String(p).indexOf('Map') >= 0)) return;
      const sig = ps.map((p) => p.getSimpleName()).join(',');
      let impl; try { impl = C[mn].overload(sig); } catch (e) { return; }
      impl.implementation = function () {
        const before = safe(() => dumpMap(arguments[0]), '');
        const ret = impl.apply(this, arguments);
        say('CALL ' + name + '#' + mn + '(' + sig + ') 入参=' + before +
            ' 返回=' + safe(() => (ret === null ? 'null' : String(ret)), '?').substring(0, 260));
        return ret;
      };
      n++;
    });
  });

  net.forEach((name) => {
    let C; try { C = Java.use(name); } catch (e) { return; }
    safe(() => C.class.getDeclaredMethods(), []).forEach((m) => {
      const mn = safe(() => m.getName(), '');
      if (!/getHashMap|getParam|buildParam|getParams|getMap/i.test(mn)) return;
      const ps = safe(() => m.getParameterTypes(), []);
      const sig = ps.map((p) => p.getSimpleName()).join(',');
      let impl; try { impl = C[mn].overload(sig); } catch (e) { return; }
      impl.implementation = function () {
        const ret = impl.apply(this, arguments);
        say('NET ' + name + '#' + mn + ' -> ' + safe(() => dumpMap(ret), ''));
        return ret;
      };
      n++;
    });
  });

  armed = n > 0;
  say('已挂 ' + n + ' 个方法｜armed=' + armed);
  return armed;
}

Java.perform(function () {
  say('注入成功');
  hookPut();
  const timer = setInterval(function () {
    tries++;
    if (arm() || tries > 40) {
      clearInterval(timer);
      if (!armed) say('超时：没找到业务加密类');
    }
  }, 1000);
});
