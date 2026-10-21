# 到梦空间活动监控（Dream Monitor）

> **现在能用的是 `watch.py`（完全不需要登录）**，见 [使用说明.md](使用说明.md)。
>
> 下文 `cli.py` 那套走的是 App 自己的加密协议，加密链路已验证正确，但因为拿不到 **App token**（Web 扫码的 token 与 App token 是两套凭证，App 接口返回 `10007 登录已失效`），暂时跑不通，留作协议备忘。

实时拉取「到梦空间」App 的活动列表，比对状态与名额变化，命中筛选条件就推送到手机。

- **状态统计**：报名中 / 已满员 / 未开始 / 进行中 / 已结束，每轮打印汇总
- **变化提醒**：新活动上线、满员后释放名额、未开始→开放报名
- **精准筛选**：关键词、类别、主办方、剩余名额、距截止时间
- **多渠道推送**：Server酱（微信）/ Bark（iOS）/ 钉钉 / 企业微信 / Telegram / 邮件 / 自建 webhook
- **可选自动报名**：默认关闭，需显式开启并限定白名单

加密链路（AES-CBC + RSA-1024 + signToken 签名）用纯标准库实现，除 `requests`/`PyYAML` 外无依赖。

---

## 30 秒跑起来

```powershell
cd C:\deepseekH-work\dream-monitor
python cli.py init          # 生成 config.yaml
python cli.py login         # 填 uid / token（获取方式见下节）
python cli.py probe         # 打印真实活动，确认字段解析正确
python cli.py test-notify   # 测推送
python cli.py run           # 开跑
```

只跑一轮看效果：`python cli.py once`

---

## 一、取 uid / token（唯一门槛，做一次）

App 的请求体整个是加密的，抓包只能看到 `d=<密文>`。uid/token 只存在于**设备内存**里，
所以要在 App 运行时从内存读出来。三条路，按省事程度排序：

### 路线 A：模拟器 + Frida（推荐，15 分钟）

1. 装 **MuMu 模拟器** 或 **雷电模拟器**（自带 root，不用折腾）
2. 装到梦空间 App，登录你的账号，点开「活动」页面确认能正常加载
3. 下载 [frida-server](https://github.com/frida/frida/releases) 对应架构（模拟器一般是 `x86_64`），
   重命名为 `frida-server`，推进模拟器：

   ```powershell
   adb push frida-server /data/local/tmp/
   adb shell "su -c 'chmod 755 /data/local/tmp/frida-server'"
   adb shell "su -c '/data/local/tmp/frida-server &'"
   ```

4. 电脑上：

   ```powershell
   pip install frida-tools
   frida -U -f com.jingcai.apps.qualitydev -l hook.js --no-pause
   ```

5. 在模拟器里点一下「活动」页，终端会打印：

   ```
   [BaseRequest.getHashMap] {uid=1234567, token=a1b2c3d4..., version=4.9.5, ...}
   >>> uid   = 1234567
   >>> token = a1b2c3d4e5f6...
   ```

6. 抄下来填进配置：

   ```powershell
   python cli.py login
   ```

`hook.js` 已经写好，同时挂了 `BaseRequest.getHashMap`、`SignUtil.getSign`、`HashMap.put`
三个点，总有一个能命中。

### 路线 B：真机 root + Frida

同上，只是 frida-server 要选真机对应架构（多为 `arm64`）。需要 Magisk。

### 路线 C：真机免 root —— adb backup 解包

只对允许备份的 App 有效，到梦空间不一定放行，成功率低，作为兜底：

```powershell
adb backup -f dm.ab -noapk com.jingcai.apps.qualitydev
# 用 abe (android-backup-extractor) 解包，再翻 shared_prefs 里的 xml
```

---

## 二、配置

```yaml
poll_interval: 30        # 轮询间隔秒。想抢活动调到 15~20，日常 60 足够
page_size: 20            # 每页条数
max_pages: 10            # 最多翻几页（20×10 = 200 条）

rules:
  states: [open, full, soon]   # 关心的状态
  require_quota: true          # 只推还有名额的
  include: ["讲座", "志愿"]     # 名称/类别/主办方 含任一即命中
  exclude: ["已结束"]
  categories: []               # 例: ["思想成长"]
  organizers: []               # 例: ["校团委"]
  min_hours_left: 1            # 距报名截止不足 N 小时不推
```

`include` 留空 = 全部活动都通知（很吵，建议至少填一两个词）。

推送渠道在 `notify:` 下，把对应项 `enabled` 改 `true` 并填 key。**Server酱最省事**：
[sct.ftqq.com](https://sct.ftqq.com) 微信扫码拿 SendKey → 填 `serverchan.key` → 微信直接收。

改完跑 `python cli.py test-notify` 验证。

---

## 三、命令

| 命令 | 作用 |
|---|---|
| `python cli.py init` | 从模板生成 config.yaml |
| `python cli.py login` | 写入 uid/token/jPushId 并立即验证 |
| `python cli.py probe` | 请求一页，dump 原始 JSON 到 `data/raw_page1.json` |
| `python cli.py once` | 跑一轮（调试） |
| `python cli.py run` | 持续监控 |
| `python cli.py test-notify` | 测试所有已启用渠道 |
| `python cli.py events` | 查看最近状态变化记录 |

---

## 四、字段对不上怎么办

App 各接口字段名不统一，`rules.py` 用候选名列表容错提取。若 `probe` 打印出
`(无标题)` 或余量明显错误：

1. 打开 `data/raw_page1.json` 看真实字段名
2. 在 `rules.py` 的 `normalize()` 里把真实字段名加到对应 `_pick(...)` 最前面

```python
name = str(_pick(a, "actName", "activityName", "name", "title", default="(无标题)"))
```

---

## 五、已知边界

- **token 会过期**。失效时打印 `凭据失效：登录已失效，请重新登录`，重抓一次即可。
  App 内「退出登录」会作废 token。
- **风控**。`request_delay` 别低于 0.8 秒，别开多实例并发扫。触发会返回 `-2000 数据出现异常`。
- **自动报名默认关闭**。它会真替你报名，报名后取消可能影响信用分。开启时务必用
  `auto_signup.only_ids` 白名单，别靠关键词裸奔。
- **状态码映射**。`3=报名中` 已实测确认，其余是按接口文案推测的。跑一段后用 `cli.py events`
  对照实际状态，再改 `status_map`。
- **范围**：只覆盖 App「活动」列表。部落活动、签到、学分明细不在内。

---

## 六、文件

| 文件 | 作用 |
|---|---|
| `dmcrypto.py` | AES-CBC / RSA-1024 PKCS1 / fastjson 序列化 / signToken |
| `dmclient.py` | API 客户端：设备指纹、限速、错误码分类 |
| `rules.py` | 活动归一化（多候选字段容错）+ 筛选匹配 |
| `store.py` | SQLite：活动快照、事件流水、通知去重 |
| `notify.py` | 七种推送渠道 |
| `monitor.py` | 轮询、diff、通知、可选自动报名 |
| `cli.py` | 命令行入口 |
| `hook.js` | Frida 脚本，从 App 内存打印 uid/token |
| `test_offline.py` | 离线仿真，验证 diff/去重/容错 |

自检：

```powershell
python dmcrypto.py      # NIST FIPS-197 + SP800-38A 向量
python test_offline.py  # 端到端逻辑仿真
```

---

## 七、协议备忘（改代码时会用到）

```
POST https://appdmkj.5idream.net/{path}
Header: standardUA=<设备指纹JSON>   user-agent: okhttp/3.11.0
Body:   d=<密文>   (application/x-www-form-urlencoded)

d = b64spaces( b64spaces(AES-CBC(明文JSON)) + "==" + base64(RSA-1024-PKCS1(session_key)) )
    session_key = 16 位随机 [A-Za-z0-9]，IV = 1628092121312213
明文JSON = TreeMap 键排序 + 紧凑无空格 + 值为字符串
signToken = MD5( SHA512(明文JSON)[1::2][0::2] ).upper()

错误码: 100 成功 / 10005 登录失效 / 11001 签名错误 / 11002 参数错误
        -2000 数据异常 / 2000011 已报名 / 2000166 不在报名期 / 2000000 活动不存在
```

关键接口：`/v2/activity/activities`（列表）、`/v2/activity/detail`、
`/v2/signup/info`、`/v2/signup/submit`（报名）。
