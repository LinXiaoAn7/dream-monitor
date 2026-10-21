// hook_noop.js — 只探活，不做任何 hook
'use strict';
console.log('[dm] script loaded');
var t = 0;
var iv = setInterval(function () {
    t += 5;
    console.log('[dm] alive at ' + t + 's');
    if (t >= 30) clearInterval(iv);
}, 5000);
