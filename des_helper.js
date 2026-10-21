// 到梦空间登录密码加密：DES-ECB + 零填充（NOT PKCS5）
//
// 关键：服务端只接受单块 8 字节密文（16 个 hex 字符）。
//   - PKCS5 碰到"正好 8 字节"的密码会多补一整个块 → 32 hex → 服务端报 11002 参数错误
//   - 所以这里用 no-auto-padding + 手工补 \0 到 8 的倍数
//   - 超过 8 字节的密码需要 trunc=1（截断到前 8 字节），因为服务端只认单块
//
// 用法: node des_helper.js <key> <plaintext> [trunc]
// 输出: 大写十六进制密文

const crypto = require('crypto');

function desEncryptZeroPad(keyStr, text, truncate = false) {
  const k = Buffer.from(keyStr, 'utf8');
  const k3 = Buffer.concat([k, k, k]); // 3DES 三密钥相同 == 单 DES

  let b = Buffer.from(text, 'utf8');
  if (truncate && b.length > 8) b = b.subarray(0, 8);

  const rem = b.length % 8;
  if (rem !== 0) b = Buffer.concat([b, Buffer.alloc(8 - rem, 0)]);

  const ci = crypto.createCipheriv('des-ede3-ecb', k3, null);
  ci.setAutoPadding(false);
  return Buffer.concat([ci.update(b), ci.final()]).toString('hex').toUpperCase();
}

const [, , key, text, trunc] = process.argv;
if (!key || text === undefined) {
  console.error('usage: node des_helper.js <key> <plaintext> [trunc]');
  process.exit(2);
}
process.stdout.write(desEncryptZeroPad(key, text, trunc === '1' || trunc === 'true'));
