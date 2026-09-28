# B 站评论抓取问题记录

> 按时间顺序记录 B 站评论抓取链路的线上 Bug 现象、根因、排查与解决方案，供复盘与复用。
> 记录模板：问题现象 → 根因分析 → 排查过程 → 解决方案 → 验证结果 → 关键收获 → 涉及文件。

---

## 2026-09-28 B 站高赞评论被风控拦截时误报「平台不支持/无评论」

### 问题现象

- 复现链路：前端解析 B 站视频 `BV1qT411Q751` 并请求「高赞评论」。
- 前端报错：

  ```text
  该平台暂不支持评论抓取，或该视频暂无评论。
  ```

- 关键矛盾：该视频**在 B 站网页上明确有高赞评论**（实际有 3 条，最高赞 1605），但服务端
  始终返回空；而**其他部分视频又能正常抓取**，表现为"时有时无"的间歇性失败。

### 根因分析

1. **直接原因——B 站风控拦截返回空数据**：
   - 用临时探测脚本对比三种请求模式（同一视频 `BV1qT411Q751`）：

     | 请求模式 | B 站响应 |
     |---|---|
     | 旧端点 `x/v2/reply/main`、**无 Cookie**（原代码行为） | `code=-352`（风控校验失败），`data` 为空 |
     | 经 `x/frontend/finger/spi` 领取 buvid3/buvid4 后重试旧端点 | `code=0`，3 条评论 |
     | WBI 签名新端点 `x/v2/reply/wbi/main` + Cookie | `code=0`，同样 3 条 |

   - 结论：**无 buvid 指纹的裸请求被 B 站风控拦截（`-352` 或 `-412`）**，
     服务端用空 `data` 响应，导致上层读到的 `replies` 为空。
2. **放大因素——旧代码不检查 code，把风控失败伪装成「无评论」**：
   - 旧 `bilibili.fetch` 仅做 `resp.raise_for_status()` + `resp.json().get("data").get("replies")`，
     **完全不看 `code` 字段**。风控返回 `code=-352` 时 data 为空，旧代码照样读 replies→[]。
   - 门面层 `fetch_comments` 把空评论统一抛成 `CommentsNotSupportedError("该平台暂不支持…")`
     ——这是「部分视频能抓、部分不能」的根本原因：风控命中与否取决于 IP/指纹/频率。
3. **偶发性表现的解释**：
   - B 站风控的命中阈值是动态的（同一 IP 短时间内多次请求、无指纹、高频换 UA 都会增加命中
     概率），因此同一代码对不同视频会出现不同结果；旧缓存里的成功结果则继续可用，进一步
     掩盖"新请求几乎全部被拦截"的真相。

### 排查过程

1. 读 `backend/comments/bilibili.py`、`backend/comments/__init__.py`、
   `backend/comments/generic.py`：确认「不支持/无评论」文案来自门面 `fetch_comments`
   对空 `comments` 的统一判断，而 bilibili 模块自身从不抛 `NotSupported`（只在 `_resolve`
   阶段抛），触发点在「bilibili.fetch 返回空 comments → 门面误判」。
2. 读 `tests/test_comments.py`：既有测试只 mock 抖音模块，bilibili 无覆盖，可安全重构。
3. 写临时探测脚本 `_probe_comments.py`：对出事视频三路对比请求，得到上表实测证据，
   确认 `-352` 风控码与 Cookie 缺失的因果关系。
4. 结论：必须**携带访客指纹 Cookie + 走 WBI 签名新端点 + 检查 code 如实报错**，才能把
   风控拦截从"被伪装成无评论"还原成"可被观测、可被重试"的真实失败。

### 解决方案

重构 `backend/comments/bilibili.py`，分四层容错（与抖音模块 `douyin.py` 风格对齐）：

1. **进程级会话 + 访客指纹**：
   - 模块级 `requests.Session`，首次请求时调 `x/frontend/finger/spi` 领取 `buvid3` /
     `buvid4` 访客 Cookie 并 set 到 `.bilibili.com` 域；
   - 领取失败不抛错（best effort），风控失败由上层如实报错。
2. **WBI 签名新端点优先**：
   - 调 `x/web-interface/nav` 拿 `img_url` / `sub_url` 的 32 位哈希，按 B 站前端固定
     置换表混淆后取前 32 位作 `mixin_key`，**缓存 1800 秒**（key 按天轮换）；
   - 对 `reply/wbi/main` 请求补 `wts`（秒级时间戳）+ `w_rid`（md5 签名），并剔除
     `!'()*` 特殊字符；
   - 签名链路异常时**回退**旧端点 `x/v2/reply/main`（仍带 Cookie）。
   - 防御加固：`len(img) != 32 or len(sub) != 32` 时放弃签名、走旧端点（nav 结构异常兜底）。
3. **风控码换指纹重试一次**：
   - `_fetch_replies` 单轮内「WBI → 失败回退 plain」，若仍命中 `_RISK_CODES = {-352, -412}`
     则 `_reset_session()` 重建会话（新 buvid）再试一轮；仍失败把 code 交给上层如实上报。
4. **如实报错（不伪装）**：
   - `code != 0` 抛 `CommentsError("B 站评论接口返回 code=…：…（多为风控拦截或接口变更，
     请稍后重试）")`，前端映射 502；
   - 只有 `code=0` 且 `replies` 与 `top_replies` 都为空时，才由门面判
     `CommentsNotSupportedError`（真正无评论）。
5. **高赞不遗漏**：
   - `top_replies`（置顶/热门评论）与 `replies`（分页列表）合并、按 `rpid` 去重，
     避免置顶评论被分页截断遗漏。
6. **可测试性**：
   - 所有网络调用收敛到 `_get_json(url, params)`，测试可 monkeypatch 该函数隔离网络；
   - `tests/test_comments.py` 新增 `test_bilibili_classification`：风控码→CommentsError
     且换指纹自动重试（4 次请求）、合并去重归一化、`code=0` 真无评论→空列表。

### 验证结果

- 离线回归：`test_comments.py` **8 passed**（含新增 B 站分类用例）；`GetProblems`
  无错误；临时探测/复验脚本 `_probe_comments.py` / `_verify_comments.py` 已清理。
- 真实链路复验：
  - **BV1qT411Q751**（此前失败视频）：成功返回 3 条（最高赞 1605、次高 1447、817）；
  - **BV1q47f66ERF**（此前正常视频回归）：成功返回 3 条（845、754、391 赞）。
- 极端情况下仍被风控时，用户看到的是**带 code 的如实提示**而非误导文案，便于后续排查。

### 关键收获

- **第三方风控拦截型 API 必须先查 code 再看数据**：空 `data` ≠ "无资源"，更常见的
  是"被拦截返回空数据"。旧代码把两者混为一谈，是这类 Bug 的共同根因。
- **开放接口必须携带访客指纹 Cookie**：B 站评论等无登录态接口依赖 `buvid3` / `buvid4`
  指纹豁免风控，裸 UA 请求几乎必被拦截。
- **风控重试时换指纹比同指纹重试更有效**：`_reset_session()` 重建 buvid 比同会话重连
  更容易通过风控校验，这是 B 站风控"基于指纹计数"特性决定的。
- **错误语义要诚实**：风控拦截 ≠ "平台不支持" ≠ "无评论"，前端拿到不同错误类型才能
  给不同引导（如：风控失败引导"稍后重试"，真无评论显示"暂无评论"）。
- **可测试性设计**：把所有网络调用收敛到单一函数（`_get_json`），测试才能通过 monkeypatch
  实现无网络断言，这也是抖音模块 `douyin.py` 的既有风格。

### 涉及文件

- `backend/comments/bilibili.py`（重构）
- `tests/test_comments.py`（新增 `test_bilibili_classification`）
