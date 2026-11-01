# 航迹｜机场一小时平均延误研究系统

面向科研与教学的机场延误网站。当前产品只预测一个明确目标：北京首都机场 PEK 在计划离港窗口 `[T, T+1h)` 内，全部有效出港航班的平均离港延误。它不是单次航班预测，也不是航线预测。

系统包含账号登录、2019 年机场小时历史留出回看、一小时研究预报、NOAA 空间天气监测、事件期—完整安静期的首段到港历史对比、可选 Aviationstack 机场数据覆盖检查，以及管理员用户/登录审计。

## 当前状态

- 关键运行资产位于 `data`：`model.joblib`、`test_windows.pkl`、`metadata.json`、`historical_space.csv.gz`、`event_arrival_comparison.json`。
- 模型、元数据和测试窗口共享 `asset_id` 与 `schema_version`；启动时不一致会直接失败，避免口径错配。
- 本地默认仅监听 `127.0.0.1`；云端通过 `HOST`、`PORT`、`PUBLIC_ORIGIN` 等环境变量开启。
- 模型等只读资产保留在 `data`，账号数据库和缓存可通过 `STATE_DIR` 挂载持久卷。
- 支持普通 12 小时会话和用户主动选择的 30 天个人设备登录。
- 公网 HTTPS 使用 Secure、HttpOnly、SameSite=Lax Cookie，并保留 CSRF、Host/Origin 校验和基础限流。

## 本地启动

要求 Python 3.12。推荐在项目目录创建虚拟环境并安装锁定依赖：

    py -3.12 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    .venv\Scripts\python -X utf8 server.py

也可以双击“启动网页.cmd”。默认地址为 http://127.0.0.1:8765。

首次使用全新状态目录时会创建 `admin`，并把随机初始密码写入状态目录下的“管理员初始账户.txt”。已有数据库不会重复创建或覆盖管理员。

### 可登录 Demo

双击 `启动Demo.cmd` 可在 `http://127.0.0.1:8898` 启动隔离的本地演示环境。演示账号保存在被 Git 和 Docker 忽略的 `.demo-state` 中，不会读取或覆盖正式账号库；本机首次初始化后的登录凭据由演示交付说明提供。

项目根目录的 `index.html`、`app.js` 与实际运行使用的 `static/index.html`、`static/app.js` 保持同步，便于继续基于本次迭代修改。

## Docker / 云端

项目包含 `Dockerfile`、`railway.json`、`.dockerignore` 和“部署说明.md”。公网部署至少需要：

- 持久卷挂载到 `/app/state`；
- `STATE_DIR=/app/state`；
- `ADMIN_INITIAL_PASSWORD` 设置为至少 10 个字符的强密码；
- `TRUST_PROXY=1`；
- `PUBLIC_ORIGIN=https://你的域名`，或使用 Railway 自动提供的 `RAILWAY_PUBLIC_DOMAIN`。

保持单副本运行。SQLite 不支持多个容器同时写入一个数据库。会休眠的实例可能有冷启动；真正的全天候即时访问需要常驻实例或持续运行的服务器。

## 账号与安全

- 用户名 3—32 位，支持中文、字母、数字、下划线和短横线，大小写视为同一用户名。
- 密码 10—128 个字符，采用 PBKDF2-HMAC-SHA256 与独立随机盐保存。
- 数据库只保存会话令牌哈希，浏览器 Cookie 不向 JavaScript 暴露令牌。
- 管理员可以查看用户状态、注册/登录来源与安全审计，但不能查看密码或密码哈希。
- 修改密码或停用账号会撤销该用户全部登录。
- `REGISTRATION_ENABLED=0` 可以关闭自主注册。
- 公网默认禁止从网页临时设置 Aviationstack 密钥；请使用 `AVIATIONSTACK_API_KEY` 环境变量。

不要把现有 `accounts.sqlite3`、管理员密码文件、`gateway_secret.txt`、日志、截图、`.deps` 或 `cloudflared.exe` 上传到代码仓库或部署镜像。

## 目标、清洗与指标

- 数据范围：2017—2019 年 PEK 全目的地出港记录；目前不能外推为其他机场。
- 目标：窗口内平均离港延误；单班离港延误 = 实际出发 − 计划出发，提前出发保留负值。
- 窗口：北京时间计划离港时刻 `[T, T+1h)`；每个建模窗口至少 5 个有效物理航班。
- 清洗：剔除取消、缺失与早于计划 60 分钟以上的异常记录；合并可识别的共享航班及经停重复记录。
- 切分：2017-01 至 2018-06 训练；2018-07 至 2018-09 历史开发评估；2018-10 至 2018-12 区间校准；2019 全年历史留出审计。后两项都已参与项目设计认识，不称为全新确认段。
- 主指标：机场小时窗口等权 MAE；同时报告 RMSE、Bias 和机场小时 80% 区间覆盖率。
- 模型 A 只考虑时间因素：使用训练期“星期 × 小时”的机场小时平均延误历史中位数。
- 模型 B 完整保留模型 A，并用梯度提升树学习滞后 Dst 变化率与 ROTI 对剩余误差的修正；空间修正权重为 `α=0.75`，缺测时逐窗口回退模型 A。
- 2018 Q3 历史开发评估：模型 A MAE 20.0451 分钟，模型 B MAE 19.7996 分钟，B 改善约 1.225%，日块 Bootstrap 的 B-A 95% 区间为 -0.391 至 -0.102 分钟。
- 2019 历史留出审计：模型 A MAE 16.0266 分钟，模型 B MAE 16.0211 分钟，点估计只改善约 0.035%；B-A 95% 区间为 -0.063 至 +0.054 分钟并跨过 0，未证明稳定增益。
- 生产启用要求同时通过历史开发内部筛查、历史留出筛查、ROTI 时区确认和全新确认段四项门槛。当前后三项中的历史留出稳定性、时区确认和全新确认段未闭合，因此主预报固定使用模型 A；模型 B 仅作研究情景展示。
- 当前模型 A 的 2019 历史留出：8,006 个机场小时，MAE 约 16.03 分钟，RMSE 约 26.60 分钟，Bias 约 0.71 分钟；名义 80% 区间实际覆盖率约 73.1%。
- “空间天气对比”页不再用 Dst/ROTI 高低四分位分组。它冻结使用 Scientific Reports 2023 补充材料 Table A 中覆盖本地数据期的 31 条事件，CME 起点补偿 30 分钟，每个事件窗固定 24 小时；重叠后形成 28 个事件簇。完整安静期必须是北京时间全天无事件窗，事件日残余时段不塞入安静组。
- 本地到港对比的研究单元是“每次 PEK 物理起飞的第一停靠站”：按最短合理计划航程确定首段，再计算实际到港减计划到港。它表示 PEK 始发队列的下游首段结果，不是到达 PEK 的入港航班，也不替换主预测的离港目标。
- 历史探索结果：事件期首段平均到港延误比完整安静期高约 7.11 分钟，≥30 分钟比例高约 3.69 个百分点；航班记录级 KS 检验可区分两组分布。按年×月×星期×计划到港小时调整并让 28 个事件簇等权后，差异约为 +4.30 分钟，5,000 次事件簇 Bootstrap 95% 区间约为 -3.39 至 +14.08 分钟并跨 0，28 簇中正负方向各 14 个。因此只能写成“航班记录层面有差异，事件层证据尚不稳定”，不能宣称稳健显著或因果成立。
- 国际上常见 3 小时机场平均延误。3 小时均值更平滑，与 1 小时目标的难度不同，不能直接混用两者 MAE。
- 当前年份超出验证期，模型不含地面天气、机场流量实况、跑道构型和前序航班，因此页面明确标记为研究估计。

## Aviationstack 数据边界

实时连接器只查询 `dep_iata=PEK`，按计划离港小时聚合 `departure.actual - departure.scheduled`。页面不会输出单航班预测。

`complete` 只表示提供商分页是否完整；每小时 `observed_flights / scheduled_flights` 才代表已有实际离港时间的观测覆盖。分页或实际字段不完整时，只能把显示值称为“样本均值”，不能宣称完整机场平均。

## 重新训练与测试

生成训练资产与科研图表前安装分析依赖：

    .venv\Scripts\python -m pip install -r requirements-analysis.txt

    .venv\Scripts\python -X utf8 modeling.py --train --roti-timezone-status unconfirmed_assumed_utc
    .venv\Scripts\python -X utf8 build_event_arrival_comparison.py
    .venv\Scripts\python -X utf8 generate_model_figures.py
    .venv\Scripts\python -X utf8 -m unittest -v test_auth.py test_system.py

测试使用临时账号数据库，不会向真实 `accounts.sqlite3` 添加用户。
