# 闲鱼低价捕捉机

一个跑在自己电脑上的闲鱼低价商品监控台：按**关键词 + 价格区间 + 折扣门槛**盯着商品流，命中后自动做**捡漏评分、去重、留痕、推送**。

Python 3.9+，**零第三方依赖**，一条命令启动，数据全部留在本机。

```powershell
python run.py
```

打开 <http://127.0.0.1:8765/>，自带两个演示任务，20 秒内就能看到第一批命中。

---

## 一、先说明「取数」这件事

闲鱼没有面向个人的公开搜索接口。任何未授权的自动化抓取都要绕过请求签名和风控，既违反平台协议（有封号风险），也随时会因为机制变化而失效，所以**本项目不内置任何签名算法、验证码处理、代理池或账号池**。

这个工具把两件事拆开：

| 环节 | 做法 |
| --- | --- |
| 取数 | 可插拔数据源。用你**有权访问**的接口，或手动粘贴，或本地 CSV |
| 判断 | 过滤、参考价、评分、去重、推送、留痕，全部本地完成 |

内置的数据源：

| 名称 | 说明 | 是否轮询 |
| --- | --- | --- |
| `mock` | 演示数据，按关键词模拟行情，用来验证整条链路。界面上会标「演示数据」 | 是 |
| `manual` | 手动粘贴导入，从闲鱼 App 复制标题和价格，零抓取 | 否 |
| `csv` | 本地 CSV 文件（`data/watch.csv`），由插件提供 | 是 |
| `http` | 通用 HTTP JSON 适配器，填接口地址和字段映射即可对接你已授权的接口 | 是 |

合规接入方式的优先级：**官方开放平台授权接口（需相应资质，以签约条款为准）> 已确认授权链路的第三方数据服务 > 手动 / CSV 导入**。不要拿个人账号的 Cookie 做高频自动化请求，那是最容易被限制账号的做法。

---

## 二、它具体做什么

1. **任务**：关键词（命中任意一个）、排除词、价格区间、最低折扣、地区、轮询间隔。
2. **参考价**（决定「便宜」的基准，按优先级）：
   - 数据源返回的行情价 `market_price`
   - 任务里手填的参考价
   - 同一任务已见商品价格的中位数
3. **评分**（0–100，S/A/B/C 分级，每个命中都写明理由）：
   - 折扣幅度 48%
   - 新鲜度 22%（6 小时内满额，之后线性衰减）
   - 卖家信用 16%
   - 附加条件 14%（包邮、同城、想要人数）
4. **去重**：同一商品在同一任务下只记一条命中，反复轮询不会刷屏。
5. **提醒**：站内弹窗 + 提示音 + 桌面通知 + Webhook（飞书/钉钉/企微）+ Bark，每一次发送都写日志。
6. **留痕**：SQLite 保存商品、价格快照、命中、推送记录；一键导出 CSV。

## 三、界面

- **左栏**：监控任务卡。可「立即运行」「只看这个」「编辑」；任务卡上直接显示价格区间、折扣门槛、间隔、累计命中。
- **中栏**：实时捕获流。按 S/A/B/C 筛选，卡片上有现价、参考价、折扣、命中理由、卖家/地区/想要人数、评分；操作为「打开原贴」「复制砍价话术」「忽略」。
- **右栏**：折扣分布图、数据源状态与连通测试、粘贴导入、推送日志。
- **顶栏**：引擎状态，可随时暂停/恢复（暂停后不再抓取，但数据都在）。

---

## 四、接入一个真实接口（HTTP 数据源）

新建任务时把「数据源」选成 `自定义 HTTP 接口`，在配置框里填：

```json
{
  "url": "https://api.example.com/search",
  "method": "GET",
  "headers": { "Authorization": "Bearer <你的 token>" },
  "params": { "q": "{keyword}", "maxPrice": "{max_price}" },
  "items_path": "data.items",
  "map": {
    "item_id": "id",
    "title": "title",
    "price": "price",
    "url": "url",
    "image": "pic",
    "seller": "seller.nick",
    "region": "area",
    "want": "wantCnt",
    "published_at": "gmtCreate",
    "market_price": "marketPrice"
  }
}
```

规则：

- 占位符支持 `{keyword}`（第一个关键词）、`{keywords}`（逗号拼接）、`{max_price}`、`{min_price}`、`{task_name}`。
- `items_path` 用点号取数组，支持数字下标，例如 `data.list.0.items`。
- `map` 里的值也是点号路径；`item_id` 缺失时用标题+价格算哈希兜底。
- `published_at` 支持秒/毫秒时间戳和常见日期字符串，统一转成东八区 ISO 时间。
- `method` 为 `POST` 时，`json_body` 作为请求体；不填则把 `params` 序列化当请求体。
- 填完点表单里的「测试数据源」，会返回连通结果和一条示例商品。

## 五、手动导入（最稳的一条真实数据路）

闲鱼没有公开搜索接口，最稳的做法是**你看到什么，就喂什么**。控制台右侧「粘贴导入」支持四种形态，自动识别：

**1. 一行一条（最常用）**

```
iPhone 13 128G 95新 自用 1899 https://www.goofish.com/item?id=xxx
RTX3060 显卡 1450
```

价格支持 `¥1,299`、`1299元`、`1299.00`。

**2. 闲鱼分享文案**（整段粘贴，自动抽链接与标题）

```
【闲鱼】https://m.tb.cn/h.6abc “iPhone 13 128G 国行 自用无磕碰” ¥1899 点击链接直接打开
```

**3. 表格文本**（从 Excel / 网页表格直接复制，逗号或制表符分隔，中英文表头都认）

```
标题,价格,链接,行情价
iPhone 14 128G 国行 95新,2000,https://www.goofish.com/item?id=9001,5000
RTX 3060 12G 显卡,1999,https://www.goofish.com/item?id=9002,3200
```

列名可用 `title/标题`、`price/价格`、`url/链接`、`market_price/行情价`、`seller/卖家`、`region/地区`、`want/想要人数`、`published_at/发布时间`。

**4. JSON**（把网页接口响应或自己的导出直接丢进来；点「导入文件」可选 .json/.csv/.txt）

```json
[{"标题":"搬家出 显卡 RTX 3070 8成新","价格":"1,780","链接":"https://www.goofish.com/item?id=9003","行情价":"3000","地区":"惠州","想要人数":26}]
```

**关于能不能命中**：评分必须有参考价。给法有三条，任选其一——表格/JSON 里带 `行情价`；任务里手填「参考价」；或者**多导入几条同款**，系统会拿同任务其他在售价格的中位数当基准。三条都没有时，商品会入库但不记命中（不会伪造折扣）。

导入后同样走过滤、评分、去重、推送。适合盯少量目标、不想碰任何抓取的情况。

## 六、推送配置

- **飞书**：群设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook 地址，粘贴到设置里。
- **钉钉 / 企业微信**：同理，选对应类型。
- **Bark**（iPhone）：填 `https://api.day.app/你的Key`。
- **Windows 桌面通知**：`pip install winotify`，不装就只有站内提醒和提示音。
- 设置里的「最低推送分数」决定多安静：默认 68 分（A 级）才推。

## 七、打包成 exe

```powershell
powershell -ExecutionPolicy Bypass -File build\build_exe.ps1
```

产物在 `dist\XianyuDealHunter.exe`，同目录会带 `plugins\`。`data\`（数据库）首次运行时在 exe 同级目录自动创建。

## 八、目录结构

```
xianyu-deal-hunter/
├─ run.py                 启动入口
├─ app/
│  ├─ config.py           配置、默认值、路径
│  ├─ store.py            SQLite：任务/商品/命中/快照/推送/设置
│  ├─ scoring.py          捡漏评分与分级
│  ├─ engine.py           轮询调度、过滤匹配、去重、事件广播
│  ├─ notifier.py         桌面/声音/Webhook/Bark 推送
│  ├─ server.py           HTTP 服务、REST、SSE
│  ├─ static/             控制台（index.html / style.css / app.js）
│  └─ sources/            数据源：mock / http / manual
├─ plugins/csv_source.py  插件示例：本地 CSV 数据源
├─ tests/test_core.py     单元测试
├─ data/hunter.db         运行后生成
└─ build/build_exe.ps1    打包脚本
```

## 九、接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/state` | 首屏全量状态 |
| GET | `/api/hits` | 命中列表（`task_id` / `grade` / `limit`） |
| POST | `/api/tasks` | 新建任务 |
| PUT | `/api/tasks/{id}` | 更新任务 |
| DELETE | `/api/tasks/{id}` | 删除任务及其命中 |
| POST | `/api/tasks/{id}/run` | 立即抓一次 |
| POST | `/api/tasks/{id}/toggle` | 启用/停用 |
| POST | `/api/hits/{id}/status` | 标记忽略 |
| POST | `/api/import` | 粘贴文本或商品数组导入 |
| POST | `/api/settings` | 保存推送/引擎设置 |
| POST | `/api/source/test` | 测试数据源连通性 |
| POST | `/api/notify/test` | 发一条测试推送 |
| GET | `/api/export.csv` | 导出命中 CSV（带 BOM，Excel 可直接打开） |
| GET | `/api/events` | SSE 实时事件流 |

## 十、常见问题

**端口被占用**：`python run.py --port 8899`。

**数据在哪 / 怎么重置**：`data/hunter.db`。停掉程序后删掉这个文件（以及 `-wal`/`-shm`）即回到初始状态。

**演示数据的数字是真的吗**：不是。`mock` 只是按关键词造出合理区间的价格，用来验证评分和推送链路；卡片上有「演示数据」标记，没有真实链接。

**打开原贴按钮是灰的**：演示数据没有链接。接入真实数据源或手动导入带链接后就会亮。

**会不会把账号搞封**：只要走授权接口或手动导入就不会。用 Cookie 高频请求不属于本项目支持范围。

**轮询间隔开多小合适**：不低于 15 秒。间隔越短越容易触发上游限流，反而拿不到数据。

**能不能多开**：可以，但建议用不同的 `--db` 分开数据，例如 `python run.py --port 8899 --db data/alt.db`。

## 十一、测试

```powershell
python -m unittest discover -s tests -v
```

覆盖：四种导入形态（逐行 / 分享文案 / CSV / JSON）、字段取值、时间归一化、评分分级、任务增删改、过滤与去重、参考价回退、手动导入、HTTP 接口（含首页渲染）。

## 许可证

MIT
