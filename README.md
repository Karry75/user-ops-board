---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: cf39b019f7b5dc24daabe04594d78564_cfdff2d0b75a11f1a59e525400248c00
    ReservedCode1: Hd5kNJL9gVp5PlUF1IybltXwS6AWXyWgs/NheteN0ZWvnfz4qrG12KhfUkE8epVOkLD1RiUSVKTDW5SSr3NPDQ5wwwpxZ/y4gMlll1dNv6BHnP8T5V3TntXIga10BMi+kbtam7MX5PaVrCQnnhhZ56OGZQTSLWaBJLW3OjuvRdagd+F6OhPIabEeTok=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: cf39b019f7b5dc24daabe04594d78564_cfdff2d0b75a11f1a59e525400248c00
    ReservedCode2: Hd5kNJL9gVp5PlUF1IybltXwS6AWXyWgs/NheteN0ZWvnfz4qrG12KhfUkE8epVOkLD1RiUSVKTDW5SSr3NPDQ5wwwpxZ/y4gMlll1dNv6BHnP8T5V3TntXIga10BMi+kbtam7MX5PaVrCQnnhhZ56OGZQTSLWaBJLW3OjuvRdagd+F6OhPIabEeTok=
---

# user_ops_board · 用户经营看板（押金划扣 & 沉默低频电池寻找）

## 在线访问

- 本站看板（在线）：https://karry75.github.io/user-ops-board/
- 全部看板作品集（导航页）：https://karry75.github.io/dashboard-portal/

## 技术速览

- **形态**：单文件静态看板（HTML + JavaScript + ECharts），数据以离线快照形式随页面加载，纯前端渲染、无后端依赖。
- **原理**：业务库（阿里云 AnalyticDB）→ Python 抽取/构建管线 → 脱敏聚合快照 → 静态页面；页面打开即渲染，支持按维度筛选与下钻。
- **用途**：用户经营看板：押金划扣预警与沉默低频电池寻找。
- **脱敏**：公开发布版本已移除数据库连接信息、账号口令与个人敏感字段，仅保留聚合指标。


独立 Flask 看板工程，只读复用 AnalyticDB 双库（业务库 `sharing-citybike-pro` + 基础库 `sharing-system-base-pro`），
**不修改** `board` / `electric` 目录，**不使用** 8092 / 8093 端口。

## 端口
- 本工程默认端口 **8095**（启动前已探测端口占用，8095 空闲；不占用 board 的 8092 与 electric 的 8093）。
- 配置项：`config/settings.py` → `BOARD_PORT`（可用环境变量 `BOARD_PORT` 覆盖）。

## 启动
```powershell
cd "D:\Marvis K\janus\user_ops_board"
.\start.ps1          # 或 python app.py
```
访问：http://127.0.0.1:8095/

## 目录
```
user_ops_board/
├─ app.py                  # Flask 入口与 API 路由
├─ start.ps1               # 启动脚本（写 logs/server.log）
├─ config/
│   ├─ settings.py         # 复用 board\.env 只读凭据（不明文输出、不写日志）
│   └─ thresholds.yaml     # 全部口径阈值（界面「口径与阈值」面板同步展示）
├─ core/
│   ├─ db.py               # 只读查询层：强制 select/with 白名单 + TTL 缓存
│   ├─ common.py           # 筛选器 / 城市产品选项 / 电池·标签·评价·接待映射
│   ├─ dataset.py          # 换电窗口聚合 / 最后换电 / 电池异常事件 / 网点坐标
│   ├─ deposit.py          # 押金板块（T-30/7/3/1 预警、失败归因、建议划扣、KPI）
│   ├─ silent.py           # 沉默低频板块（沉默判定、L1-L5、流通异常、动作队列）
│   ├─ user360.py          # 用户 360 明细（一行一协议 + 综合处置建议）
│   ├─ strategy.py         # 策略价值条（押金漏斗 + 客服接待成效）
│   └─ export.py           # Excel 导出（openpyxl）
├─ templates/index.html    # 看板页面（四个 Tab + 阈值面板）
├─ static/app.js           # 前端逻辑
└─ output/                 # 导出的 Excel 落盘目录
```

## 板块与口径
1. **押金板块**：足额划扣 T-30/T-7/T-3/T-1 + 已逾期分层下钻；失败归因（支付流水 / 周期代扣签约 / 还电回执 + 客诉原因组合推导）；
   失败协议按「失败原因 + 近 90 天消费 + 用户标签」评估建议可划扣最大金额；KPI 含应扣/成功/失败/失败率/挽回。
2. **沉默低频·电池寻找**：沉默 = 近 90 天 0 次换电 + 协议生效中 + 已欠租，剔除流通异常；
   低频 L1–L5 按生效天数 + 窗口换电次数 + 电量分层；动作队列（劝退 / 引导换电或退订 / 超运营范围拦截）。
3. **用户 360 明细**：一行一份协议，覆盖城市/产品/线路/用户/协议/套餐/押金预警/换电频次/消费/电池定位/
   客服备注/标签/评分/综合处置建议，支持导出 Excel。
4. **策略价值条**：押金应扣→成功→失败→挽回漏斗；客服接待（`t_reception_log.detail`）劝退与引导换电成效
   （接待后 30 天内恢复换电即为成效）。

## 接口
| 接口 | 说明 |
|---|---|
| `/api/meta` | 筛选器选项（城市/产品/OEM）+ 阈值 + 数据源 |
| `/api/thresholds` | 全部口径阈值 |
| `/api/deposit/overview` | 押金 KPI + 四段预警分层 |
| `/api/deposit/warn` | 押金预警明细（stage/keyword/page/size） |
| `/api/deposit/failure` | 失败归因分布 |
| `/api/deposit/suggest` | 失败协议建议可划扣金额 |
| `/api/silent/summary` | 沉默低频总览 + L1-L5 |
| `/api/silent/silent` `/api/silent/lowfreq` `/api/silent/actions` | 沉默线索 / 低频线索 / 动作队列 |
| `/api/user360` | 用户 360 明细（advice/keyword/page/size） |
| `/api/strategy` | 策略价值条 |
| `/api/export?type=` | Excel 导出：`deposit_warn` / `deposit_suggest` / `silent` / `lowfreq` / `user360` |

## 只读与安全
- 统一走 `core/db.py` 的 `query()` / `query_dicts()`，仅允许 `select` / `with` 开头的语句；凭据仅驻留内存，不入日志与前端。
- 所有阈值集中在 `config/thresholds.yaml`，界面同步展示，可直接调整后刷新。

## 实测记录（2026-09-23，全量口径、无筛选）
| 接口 | 首次耗时 | 返回量级 |
|---|---|---|
| `/api/health` | <1s | DB 连通 |
| `/api/deposit/overview` | 5.2s | 应扣 6885 人 / 375.7 万元，失败 737 人（失败率 10.7%），挽回 1227 笔 |
| `/api/deposit/failure` | 2.5s | 代扣签约失效 618 笔（83.9%）居首 |
| `/api/deposit/warn` `/api/deposit/suggest` | 1.8s | 预警明细 / 建议划扣明细 |
| `/api/silent/summary` | 46.3s | 活跃协议 25125，沉默 2714，低频 6549（L4 6469） |
| `/api/silent/actions` | 21.7s | 劝退 762 / 引导 5787 / 超范围 931 |
| `/api/user360` | 3.4s | 25905 份协议（SQL 分页） |
| `/api/strategy` | 9.4s | 押金漏斗 + 客服成效（近 90 天接待 15746） |
| `/api/export` | 3–33s | 5 类 Excel（最大 用户360 5.9MB / 25905 行） |

首次访问某板块会构建全量缓存（TTL 180–600s），此后重复查询走缓存；`/api/silent/actions` 单次响应约 2.7MB，
如需进一步压缩可对队列做分页。
*（内容由AI生成，仅供参考）*
