# 航迹网站：GitHub → Railway 部署步骤

> 更新日期：2026-11-01  
> 适用部署包：`航迹_Railway_GitHub_20261101_最终版.zip`

## 一、上传到 GitHub

1. 解压部署包。不要把 ZIP 文件本身作为网站源代码上传。
2. 在 GitHub 新建一个私有仓库，例如 `HangJi-Railway`，不要勾选自动生成 README 或 `.gitignore`。
3. 进入仓库后选择 **Add file → Upload files**。
4. 把解压目录中的全部内容拖入上传区域。必须保证 `Dockerfile`、`railway.json`、`requirements.txt`、`server.py` 和 `data`、`static` 文件夹位于仓库根目录，而不是再套一层文件夹。
5. 提交到 `main` 分支。不要上传管理员密码、API Key、`.demo-state`、`.deps` 或任何 `.env` 文件。

## 二、在 Railway 创建服务

1. 打开 <https://railway.com/new>，选择 **Deploy from GitHub repo**。
2. 如果尚未连接 GitHub，按提示安装 Railway GitHub App，并只授权刚才的仓库。
3. 选择仓库后，点击 **Add Variables**，先不要点击 Deploy。这样会先创建服务，便于在首次启动前完成域名、变量和持久卷配置。
4. 进入服务 **Settings → Networking → Public Networking**，点击 **Generate Domain**。程序公网启动时必须获得这个域名；如果先部署而尚未生成域名，安全校验会主动停止进程。
5. Railway 会自动检测仓库根目录的 `Dockerfile`。正式构建日志应出现 `Using detected Dockerfile`。

## 三、配置环境变量

生成域名后，在服务的 **Variables** 页面添加：

| 变量 | 建议值 | 是否必需 |
|---|---|---|
| `ADMIN_INITIAL_PASSWORD` | 自行生成的强密码，至少 10 个字符 | 首次部署必需 |
| `STATE_DIR` | `/app/state` | 必需 |
| `TRUST_PROXY` | `1` | 必需 |
| `SETTINGS_READONLY` | `1` | 建议 |
| `REGISTRATION_ENABLED` | `0` 仅管理员登录；需要开放注册时改为 `1` | 建议 |
| `AVIATIONSTACK_API_KEY` | 你的 Aviationstack Key | 可选 |

说明：

- 不需要手动设置 `PORT`，Railway 会注入；程序会直接监听该端口。
- 不需要把管理员密码或 API Key 写入 GitHub。
- Railway 会提供 `RAILWAY_PUBLIC_DOMAIN`，程序会自动识别；通常无需另设 `PUBLIC_ORIGIN`。
- 首次创建管理员后，后续修改 `ADMIN_INITIAL_PASSWORD` 不会自动覆盖持久卷中的管理员密码。

## 四、挂载持久卷

账号、登录会话和缓存必须放在持久卷，否则重新部署后会丢失。

1. 回到 Railway 项目画布。
2. 右键网站服务，选择 **Attach Volume**；也可以通过命令面板创建 Volume 并连接该服务。
3. Mount Path 必须填写：`/app/state`。
4. 保持单副本运行。项目中的 `railway.json` 已配置 `numReplicas=1`，SQLite 不适合多副本同时写入。

## 五、部署并生成公网地址

1. 确认已经生成公网域名、添加变量，并把 Volume 挂载到 `/app/state`。
2. 检查 staged changes，点击 **Deploy**。
3. 构建成功后，服务会访问 `/api/health`；返回 2xx 才会切换为新版本。
4. 打开此前生成的 HTTPS `*.up.railway.app` 地址，使用用户名 `admin` 和你设置的 `ADMIN_INITIAL_PASSWORD` 登录。

如果之前误点了 Deploy，首次失败不会损坏数据；完成域名、变量和 Volume 配置后选择 **Redeploy** 即可。

## 六、部署失败时优先检查

- **Dockerfile validation failed**：确认 Dockerfile 中没有 `VOLUME` 指令。
- **Required mount path missing**：确认 Volume 已连接到当前服务，路径严格为 `/app/state`。
- **首次公网部署必须设置 ADMIN_INITIAL_PASSWORD**：在 Variables 中补充至少 10 个字符的强密码并重新部署。
- **公网监听需要设置 PUBLIC_ORIGIN 或 ALLOWED_HOSTS**：通常是还没有执行 Generate Domain；先生成 Railway Domain，再重新部署。
- **缺少 data/... 运行资产**：确认 GitHub 仓库的 `data` 目录包含模型、测试窗口、元数据、历史空间天气和事件对比 JSON。
- **健康检查失败**：确认服务使用 Railway 注入的 `PORT`，并检查 Deploy Logs 中的 Python 启动错误。
- **GitHub 更新未自动部署**：确认 Railway GitHub App 对仓库有权限，并确认服务 Source 连接的是 `main` 分支。

## 七、官方参考

- GitHub 仓库作为服务源：<https://docs.railway.com/services>
- Dockerfile 构建：<https://docs.railway.com/builds/dockerfiles>
- 环境变量：<https://docs.railway.com/variables>
- 持久卷：<https://docs.railway.com/volumes>
- 公网域名：<https://docs.railway.com/networking/public-networking>
- 健康检查：<https://docs.railway.com/deployments/healthchecks>

> Railway 的旧 `railway.json` Config as Code 机制计划于 2026-12-01 后弃用。当前文件仍可工作；即使 Railway 后续只把它作为兼容配置，Dockerfile 仍会自动检测。届时请在 Railway 控制台保留 `/api/health`、单副本和 `/app/state` Volume 设置。
