# 航迹｜航班与空间天气研究系统

面向科研与教学的航班延误研究网站。系统包含账号登录、2019 年独立测试集回测、研究性延误估计、NOAA 空间天气监测、可选实时航班接口，以及管理员用户/登录审计。

## 当前状态

- 关键运行数据已打包在项目内：model.joblib、test_flights.pkl、metadata.json、historical_space.csv.gz。
- 本地默认仅监听 127.0.0.1；云端通过 HOST、PORT、PUBLIC_ORIGIN 等环境变量开启。
- 模型等只读资产保留在 data，账号数据库和缓存可通过 STATE_DIR 单独挂载持久卷。
- 支持普通 12 小时会话和用户主动选择的 30 天个人设备登录。
- 公网 HTTPS 下使用 Secure、HttpOnly、SameSite=Lax Cookie，并保留 CSRF、Host/Origin 校验和基础限流。
- 忘记管理员密码可在服务器控制台运行 python manage.py reset-admin。

## 本地启动

要求 Python 3.12。推荐在项目目录创建虚拟环境并安装锁定依赖：

    py -3.12 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    .venv\Scripts\python -X utf8 server.py

也可以双击“启动网页.cmd”。地址为 http://127.0.0.1:8765。

首次使用全新状态目录时会创建 admin，并把随机初始密码写入状态目录下的“管理员初始账户.txt”。已有数据库不会重复创建或覆盖管理员。

## Docker / 云端

项目包含 Dockerfile、railway.json、.dockerignore 和“部署说明.md”。公网部署至少需要：

- 持久卷挂载到 /app/state；
- STATE_DIR=/app/state；
- ADMIN_INITIAL_PASSWORD 设置为至少 10 个字符的强密码；
- TRUST_PROXY=1（平台位于可信 HTTPS 反向代理后）；
- PUBLIC_ORIGIN=https://你的域名，或由 Railway 的 RAILWAY_PUBLIC_DOMAIN 自动提供。

保持单副本运行。SQLite 不支持多个容器同时写入同一个数据库。会休眠的免费实例可能有冷启动；真正的全天候即时访问需要常驻实例或持续运行的服务器。

## 账号与安全

- 用户名 3—32 位，支持中文、字母、数字、下划线和短横线，大小写视为同一个用户名。
- 密码 10—128 个字符，采用 PBKDF2-HMAC-SHA256 与独立随机盐保存。
- 数据库只保存会话令牌哈希，浏览器 Cookie 不向 JavaScript 暴露令牌。
- 管理员可以查看用户状态、注册/登录来源与安全审计，但不能查看密码或密码哈希。
- 修改密码或停用账号会撤销该用户全部登录。
- REGISTRATION_ENABLED=0 可以关闭自主注册。
- 公网默认禁止从网页临时设置 Aviationstack 密钥；使用 AVIATIONSTACK_API_KEY 环境变量。

不要把现有 accounts.sqlite3、管理员密码文件、gateway_secret.txt、日志、截图、.deps 或 cloudflared.exe 上传到代码仓库或部署镜像。

## 模型与数据边界

- 目标：到港延误 = 实际到达 − 计划到达，提前到达保留负值。
- 训练：2017-01 至 2018-06；模型选择：2018-07 至 2018-09；区间校准：2018-10 至 2018-12；独立测试：2019 全年。
- 当前默认选择基础航班模型；空间天气模型未达到验证集至少降低 MAE 1% 的门槛。
- 2019 独立测试 MAE 约 31.7 分钟，且未优于简单航线中位数基准；名义 80% 区间实际覆盖率也低于 80%。
- 当前年份超出验证期，且模型不含地面天气、流量、前序航班等重要特征，因此页面明确标记为研究估计。
- 实时北京站 ROTI 尚未接入；缺失时回退基础模型，不用零值或 Kp 冒充。

## 测试

    .venv\Scripts\python -X utf8 -m unittest -v test_auth.py test_system.py

测试使用临时账号数据库，不会向真实 accounts.sqlite3 添加用户。
