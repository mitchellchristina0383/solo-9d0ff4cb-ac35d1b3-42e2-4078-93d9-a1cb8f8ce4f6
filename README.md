# Shared Deployment Release Rehearsal

一个用 FastAPI + SQLite 实现的发版前归属演练服务，模拟“应用发布器”和“平台发布器”修改同一个 Kubernetes Deployment 时的字段共管、退出、冲突退回和显式接管。

服务不访问真实 Kubernetes 集群，只保存演练后的合并配置和字段归属；配置范围严格限定为：

- `replicas`：副本数；
- `images.<容器名>`：按容器名区分的镜像。

## 核心规则

1. **同值共管**：当前字段已有管理方时，如果新管理方提交完全相同的值，则把该管理方加入归属列表。
2. **省略即退出**：每份声明是该管理方“当前仍管理字段”的完整集合。新声明省略（或显式传 `null`）其当前管理的字段时，该管理方退出此字段。
3. **最后一方退出才删除**：共管字段的一方退出后，只要还有其他管理方，合并配置和当前值保留；最后一个管理方退出时，才从合并配置中删除字段。
4. **他方改值整份退回**：如果提交值与最新已生效值不同，且字段还有提交方之外的管理方，则返回 `409 Conflict`。响应列出每个冲突字段、当前值、提交值以及管理方；整份声明不产生任何配置或归属变更。
5. **显式接管**：请求中设置 `"takeover": true` 后，可以修改这些冲突字段；成功后，本次发生冲突并改值的字段只归提交方。同值字段不会被独占，仍保持共管。
6. **并发按最新状态判断**：每个声明在 SQLite 上使用 `BEGIN IMMEDIATE` 先取写锁，再读取最新已提交状态，随后在同一个事务中写入配置、归属和版本号。即使使用多个 Uvicorn 进程，也由 SQLite 写事务串行化。

## Docker Compose 启动

```bash
docker compose up --build
```

打开：

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

SQLite 数据库保存在 Compose 命名卷 `rehearsal-sqlite` 中，容器内路径为 `/data/rehearsal.db`。

## 在 `/docs` 中演练共管后退出

进入 `POST /claims` → **Try it out**。可以给每次演练使用不同的 `deployment` 名称以获得干净状态。

### 1. 应用发布器创建字段

```json
{
  "manager": "app-publisher",
  "deployment": "demo",
  "replicas": 3,
  "images": {
    "web": "registry.example/web:1.27.0"
  }
}
```

成功响应中的关键字段：

```json
{
  "version": 1,
  "config": {
    "replicas": 3,
    "images": { "web": "registry.example/web:1.27.0" }
  },
  "ownership": {
    "replicas": ["app-publisher"],
    "images": { "web": ["app-publisher"] }
  }
}
```

### 2. 平台发布器提交同值声明，形成共管

```json
{
  "manager": "platform-publisher",
  "deployment": "demo",
  "replicas": 3,
  "images": {
    "web": "registry.example/web:1.27.0"
  }
}
```

此时 `ownership` 中两个字段都会包含：

```json
["app-publisher", "platform-publisher"]
```

### 3. 平台发布器提交空声明，退出其管理的全部字段

```json
{
  "manager": "platform-publisher",
  "deployment": "demo"
}
```

因为应用发布器仍是最后管理方，所以配置不会被删除；响应中可以看到 `changes.released` 包含 `images.web` 和 `replicas`，归属只剩 `app-publisher`。

随后可在 `GET /state?deployment=demo` 中查看合并配置和归属。

### 4. 冲突和显式接管

应用发布器仍管理 `replicas: 3` 时，其他管理方直接改副本数：

```json
{
  "manager": "infra-publisher",
  "deployment": "demo",
  "replicas": 9
}
```

会得到 `409`，响应体包含：

```json
{
  "code": "ownership_conflict",
  "conflicts": [
    {
      "field": "replicas",
      "submitted": 9,
      "current": 3,
      "managers": ["app-publisher"],
      "other_managers": ["app-publisher"]
    }
  ]
}
```

整份请求不会生效。显式接管：

```json
{
  "manager": "platform-publisher",
  "deployment": "demo",
  "replicas": 5,
  "takeover": true
}
```

成功后 `replicas` 的归属变为 `["platform-publisher"]`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/claims` | 提交声明；请求体中用 `deployment` 指定演练对象，默认 `default` |
| `GET` | `/state?deployment=default` | 查看合并配置、归属和版本号 |
| `POST` | `/deployments/{deployment}/claims` | 路径指定 Deployment 的提交入口 |
| `GET` | `/deployments/{deployment}/state` | 路径指定 Deployment 的状态入口 |
| `DELETE` | `/state?deployment=...` | 清空某个演练对象，仅删除本服务的 SQLite 演练状态 |
| `GET` | `/health` | 健康检查 |

为了兼容常见调用方式，也保留了 `/claim`、`/api/claims`、`/api/v1/claims` 等别名。请求还接受：

- `manager` / `owner` / `publisher` / `manager_id` 等管理方字段；
- 嵌套 `declaration`、`claim`、`payload`；
- `container_images` 或 Kubernetes 风格 `containers: [{"name": "...", "image": "..."}]`；
- `spec.replicas` 与 `spec.template.spec.containers` 的简化提取；
- `takeover` / `take_over` / `force` / `override`。

标准响应同时提供 `config` 和同义字段 `merged_config`。

## 本地开发运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# SQLite 默认写入当前目录 rehearsal.db；可用 DB_PATH 覆盖
DB_PATH=./rehearsal.db uvicorn app.main:app --reload --port 8000
```

## 自动化实测脚本

`scripts/verify_flow.py` 只使用 Python 标准库，可直接对运行中的服务执行：

```bash
python scripts/verify_flow.py http://127.0.0.1:8000
```

脚本使用每次随机的 Deployment 标识，覆盖：

- 创建、同值共管；
- 部分省略退出、一方退出不删除、最后一方退出删除；
- 单字段和多字段冲突、冲突管理方列表、整份不生效；
- 显式接管及接管后的最新归属；
- 8 个管理方并发提交同值声明，确认没有丢失归属；
- 并发执行“显式接管改值”和“非接管改值”，确认恰好一个成功、一个按最新状态返回 `409`，最终值和独占归属正确。

最后一次针对交付代码的完整运行结果为 **35 项检查全部通过**；事务实现稳定后，也曾分别在单 Uvicorn 进程和 `uvicorn --workers 2` 下完整复测通过。

## 数据与并发设计

SQLite 中有三张核心表：

- `deployment_state`：每个演练 Deployment 的单调版本号；
- `config_field`：扁平字段键和 JSON 值，例如 `replicas`、`images.web`；
- `field_owner`：字段管理方及共管顺序。

提交事务的关键顺序是：

```sql
BEGIN IMMEDIATE;
-- 读取最新 version/config_field/field_owner
-- 纯函数计算冲突、退出、共管、接管和最终状态
-- 写入新的 deployment_state/config_field/field_owner
COMMIT;
```

`BEGIN IMMEDIATE` 在读取前获取写锁，因此等待中的并发提交会在前一个事务提交后读取最新状态，不会基于旧归属误判。

## 交付核对

已实现：

- FastAPI + Swagger `/docs`；
- 副本数和按容器名镜像的合并配置；
- 同值共管、省略退出、最后一方退出删除；
- 冲突字段、当前值、提交值和管理方列表；
- 整份原子退回；
- 显式接管及冲突字段独占；
- SQLite 配置与归属同事务提交；
- 基于最新已生效状态的并发冲突判断；
- Dockerfile、Docker Compose、健康检查和命名卷；
- 端到端验证脚本。

已实测：

- `scripts/verify_flow.py`：35/35 通过；
- 单进程 Uvicorn 通过；
- 双 Uvicorn worker 的跨进程 SQLite 并发复测通过；
- `/docs` 与 `/openapi.json` 均返回 `200`；
- `python -m compileall app scripts` 通过；
- `docker compose -f docker-compose.yml config` 校验通过。

当前环境未完成/未执行：

- 此沙箱中没有 Docker daemon，且用户命名空间创建被拒绝（`unshare -Ur` 返回 `Operation not permitted`），因此没有实际执行 `docker compose up --build`；已用等价的本地 Uvicorn（含双 worker）完成运行和并发实测，Compose 文件已通过 `config` 校验。
- 未实现真实 Kubernetes API apply/回滚；本服务按需求定位为发版前演练和归属状态服务。
- 未加入鉴权和多用户权限控制；当前面向本地/受信演练环境。
