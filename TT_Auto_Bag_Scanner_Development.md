# TT Auto Bag Scanner | 开发与验收规格书

**版本：V1.0 · 2026-11-26 · [仓库编号已脱敏] · Windows 本地应用**

文档用途：供开发人员/Codex 实施、代码评审、现场联调和上线验收。本文是设计规格，不是已完成的软件。所有标为“建议”的参数需由现场负责人在试运行前确认。

## 1. 项目目标与边界

将出库包裹报表转为按快递公司分组的待扫描队列，在实物归属已核实、操作人员授权的前提下，使用 USB 连接的 iData K1 PDA 操作 JD WMS 单排序页面，自动提交物流运单、验证结果、处理常见重复组包异常并生成可追溯记录。

- V1 必须支持：设备检测、手动登录、Excel 导入、筛选、去重、预览、快递公司/排序码/大包配置、实物核验、逐条扫描、自动切换、重复异常恢复、暂停/停止/续扫、历史和导出。
- 禁止：自动关闭容器、自动组发运、凭报表状态推定实物已入包、伪造扫码记录、自动修改库存、自动重试结果不明的提交、保存 WMS 密码或绕过权限。
- 初版单电脑单 PDA 单执行线程；多设备并行、云数据库、WMS API 直连和自动发运不纳入 V1。
- 现场职责：操作员确认实物与目标容器；软件只负责受控录入与验证；仓库主管批准上线与关闭容器。

## 2. 已完成验证与尚未确认事项

| 编号 | 验证项 | 实际证据与结论 |
|---|---|---|
| V01 | USB 连接 | iData K1 序列号 [设备序列号已脱敏]；Android 11 / SDK 30；ADB 37.0.1 在 ADB_LIBUSB=1 后识别设备；uiautomator2 3.7.1 连接成功。 |
| V02 | WMS 导航 | com.ss.android.pda 1.1.23；首页 [仓库ID已脱敏]；组大包 → 集装箱化 → 单排序。XPath 点击成功，原生 text selector 曾报 StaleObjectException。 |
| V03 | 打开大包 | send_keys(clear=True) + Enter；[大包A已脱敏] → [排序码A已脱敏]，总包裹数量 [已脱敏]；[大包B已脱敏] → [排序码B已脱敏]，总包裹数量 [已脱敏]。 |
| V04 | 重复组包 | [运单A已脱敏] 提交后 WMS 返回“已扫描包裹已在当前大包中”，数量保持不变，存在 Logid 与独立弹窗输入框。 |
| V05 | 重复异常恢复 | 弹窗内直接输大包号报“运单号码与之前的不一致”；重新输入原运单号后弹窗消失，输入框恢复空白及焦点，数量保持不变。该路径已验证一次，不代表所有异常均如此。 |
| V06 | 新增扫描 | [运单B已脱敏] 提交后出现在“最后扫描的”第一条记录；显示扫描数量 [已脱敏]，无异常弹窗。总包裹数量 [已脱敏] 与扫描数量 [已脱敏] 口径未确认，不将两者之差视为成功数量。 |
| V07 | 切换大包 | Android back → 仅显示单排序 → 输入新大包号；承运商A → 承运商B 的全流程测试成功，未调用关闭容器。 |

来源：本次对话中的 2026-11-26 PowerShell/uiautomator2 实测日志。示例编号仅用于说明，不应硬编码或重新提交。

未确认：报表完整字段与真实时间格式、时区、各承运商全部排序码、WMS 完整错误字典、其他语言界面标签、成功记录与后台对账一致性、长期稳定性及设备版本兼容性。13 项核心操作检查通过不等于生产验收通过。

## 3. 技术选型与架构

| 层 | 选型 | 实现责任 |
|---|---|---|
| UI | PySide6 / Qt Widgets | Windows 原生桌面窗口、表格、状态及日志；UI 线程不执行 ADB 或文件重任务。 |
| 执行 | Python + QObject worker / QThread | 单 worker 持有 PDA，串行扫描；线程安全暂停/停止标志；signal 更新 UI。 |
| 设备 | ADB + LibUSB + uiautomator2 | 设备枚举、连接、截图、XML、输入和返回。scrcpy 仅供观察，可选。 |
| 数据 | openpyxl + datetime / zoneinfo | 只读 XLSX、字段映射、时间标准化、筛选、冲突识别。 |
| 持久化 | sqlite3 | 本地事务、任务快照、状态、审计、崩溃恢复；本机磁盘 WAL。 |
| 打包与测试 | PyInstaller / pytest | Windows x64 构建，先 onedir；锁定依赖；离线 fixture 测试与真机验收。 |

开发环境已实际使用 Python 3.14；正式构建先验证 PySide6、PyInstaller、uiautomator2 全部兼容，再锁定 Python 版本（必要时使用 3.12/3.13，属待确认工程决策）。不自研 Android 自动化协议。优先评估开源库、许可证和维护状态，再编写业务逻辑。

架构：UI → Application Services（ImportService / TaskService）→ ScanWorker → WmsController → DeviceAdapter → PDA；TaskRepository 独立维护 SQLite；StateDetector 与 FilterEngine 为可离线测试的纯逻辑模块。禁止 UI 直接调用设备对象。

目录建议：

```text
TT-Auto-Bag-Scanner/
  main.py  pyproject.toml  requirements.lock  README.md  AGENTS.md
  src/
    domain/       models.py  states.py  rules.py
    application/  import_service.py  task_service.py  scan_worker.py
    infrastructure/ device_manager.py  wms_controller.py  state_detector.py
                    exception_handler.py  excel_parser.py  repository.py
    ui/           main_window.py  carrier_settings.py  history.py
  tests/          unit/  integration/  fixtures/  e2e/
  migrations/     001_initial.sql
  packaging/      scanner.spec
```

运行数据放 `%LOCALAPPDATA%/TT-Auto-Bag-Scanner/` 下的 data、logs、reports、config；不依赖当前工作目录，不直接在 OneDrive 同步目录运行 SQLite。应用配置与历史数据分离。

## 4. 功能需求、实现与单项验收

每项以 F 编号关联第 9 节测试用例。P0 为上线必需，P1 为首版建议功能。

### F01 设备发现与连接（P0）

输入：ADB 路径、设备序列号。实现：通过受控 subprocess 参数列表执行 `adb devices -l`，区分 device/unauthorized/offline；多设备要求用户选择。Windows 启动前检查 ADB_LIBUSB=1，必要时提示安装驱动和授权 USB 调试；不得无提示 kill-server 影响其他应用。`u2.connect(serial)` 后读取设备信息、前台包名；连接超时与断线事件转换为状态。独占执行锁绑定序列号；运行中只允许一个设备调用线程。启动前核验序列号，不固定为 [设备序列号已脱敏]。

验收：连接/拔线/授权拒绝/两台设备均能明确显示状态；断线后不发新指令；恢复后不能自动续扫不明结果。

### F02 WMS 启动、登录及导航（P0）

实现：`app_start('com.ss.android.pda')`；检测登录页面时提示操作员手动登录，不读取或保存密码。识别仓库 ID `com.ss.android.pda:id/wms_house_name_text` 并与任务配置核对。首页优先原生 resource-id + 文本；WebView 使用 XPath `//*[@text="组大包"]` / `//*[@text="单排序"]`；每次跳转等待目标页面状态，不使用固定时间作为唯一依据。首次版本锁定已验证中文标签，英文标签表需现场采集后启用。异常页面暂停，不盲按返回键。

验收：从已登录正确仓库进入单排序；错误仓库、未登录、页面未加载、找不到控件时阻止扫描。不会进入混合排序、关闭容器或组发运。

### F03 Excel 导入和字段校验（P0）

实现：文件选择器仅接收 .xlsx；`load_workbook(read_only=True, data_only=True)` 不写原报表；用户选择 sheet/表头行（可自动建议）；预览并要求映射确认。最低字段：物流运单号、包裹状态、出库时间、快递公司；包裹号、排序码（如存在）、订单号等为可选追踪字段。保留原始文件 SHA-256、文件名、sheet、行号、导入时间、字段映射。单元格为公式且无缓存值、日期无法解析、缺字段或数值型运单可能失去精度时，标记数据异常而非猜测。Excel 数值超过 15 位可能已丢失精度；要求文本型运单号或重新导出。忽略全空行；未知格式报告行号。

验收：正常报表导入成功；缺列、重复表头、错误日期、空运单和损坏文件均有可理解提示；长运单、前导零不被转换为浮点数；源文件哈希前后不变。

### F04 筛选、去重及排序（P0）

实现：冻结导入或生成任务时刻 as_of；现场确认报表时区（建议 America/New_York，不能默认等于系统时区）。仅 trim 后严格等于“复核完成”；age_minutes=(as_of_utc−outbound_utc).total_seconds()/60；阈值用户可改（建议默认 30 分钟，边界 >=）；缺失、未来时间、夏令时歧义进入待核查，不自动扫描。运单保留字符串、trim，不擅自改变大小写；同运单同渠道同配置的重复行合并，取最早合法出库时间并保留所有源行；不同渠道/状态/目标冲突的重复行隔离（先检查全表冲突，再筛选），不得随意择一。按已确认的 carrier/sort/container 分组，组内出库时间升序、源行号稳定排序。用户确认任务后锁定快照，修改条件需生成新任务。

验收：阈值 30 时 29:59 排除、30:00 纳入；非复核完成排除；重复只生成一个队列项；跨渠道冲突不提交；相同输入和 as_of 生成相同排序。

### F05 快递公司、排序码、大包配置（P0）

实现：配置字段 carrier_key、display_name、alias、expected_sort_code、container_no、enabled、updated_at、version。示例 承运商A/[排序码A已脱敏]/[大包A已脱敏]；承运商B/[排序码B已脱敏]/[大包B已脱敏]；只作测试历史，不作为生产默认值。一个 carrier 可对应多个 sort code；若报表无 sort 且多渠道匹配，要求人工选择及实物核实。容器号非空、配置键唯一、不同渠道共用容器默认禁止。保存前校验；执行前逐包打开并核对 WMS 实际字段；运行中不可修改任务快照。

验收：缺配置、禁用、排序码不一致、歧义映射均不能开始对应任务；配置修改不影响已冻结任务。

### F06 预览与实物核验（P0）

实现：展示原始行数、排除原因、候选、冲突、按渠道待处理数量；支持查询及导出。每条提交前必须有可信的实物验证事件：现场扫码核对运单及实体容器标签，或受控的逐条人工确认（操作员、时间、目标大包、证据引用）。批量导入报表只生成候选，不是实物验证证据。未核验状态 WAIT_PHYSICAL，不允许提交；若容器配置变更或实物移走，原验证作废。预览/模拟模式默认不连接或不调用写入动作；真实模式由授权人员明确确认启动。

验收：未核验包裹无法提交；扫描实体标签不一致时阻止；核验信息可审计；模拟模式设备写调用次数为 0。

### F07 WMS 页面状态识别（P0）

实现：`dump_hierarchy()` → ElementTree；仅解析 package=com.ss.android.pda、可见节点，抽取 label/value、输入框、焦点、弹窗、最近记录列表。识别 LOGIN、HOME、CONTAINER_MENU、SINGLE_EMPTY、BAG_READY、SCAN_RESULT、DUPLICATE_DIALOG、UNKNOWN。解析大包号/排序码时按标签相邻结构定位；最近记录必须是可见结果区 TextView，不能匹配 EditText 或错误提示。状态识别失败一律 UNKNOWN。保存脱敏 XML fixture 做回归测试。

验收：已知页面全部正确分类；含相同运单的错误文本不被认作成功；遮挡、软键盘布局变化及多输入框场景不会盲目输入。

### F08 打开、验证及切换大包（P0）

实现：初始页仅单排序、无排序码/大包号、唯一启用且可见的空 EditText；根据新鲜 XML 动态计算 bounds 中心，点击后验证焦点，send_keys(bag, clear=True) 后 Enter；轮询直到 BAG_READY 并校验 actual_bag == expected_bag、actual_sort == expected_sort。成功后将 bag context 与会话 token 绑定。切换前确认无在途提交、无弹窗；`d.press('back')` 后验证 SINGLE_EMPTY；若先收起键盘，重新检测状态，只有确定仍为同一稳定页面才允许有限次返回；未知则暂停。打开下一包。永不点击关闭容器。成功结果页可能不展示大包号，因此依赖刚确认的独占会话；如果应用重启、用户接管、断线、状态不可证明，重新验证大包后才继续。

验收：承运商A→承运商B→承运商A 测试容器均匹配；关闭容器调用为 0；错误排序码、异常弹窗、会话失效均阻止后续提交。

### F09 单条提交与成功验证（P0）

实现：预检实物证据、任务快照、设备及会话、空且有焦点的输入框；先提交本地 IN_FLIGHT 事务及 attempt_id，再输入运单并 Enter 一次。发送后只轮询读取，不再次提交。检测“最后扫描的”结果区第一条运单与当前运单完全一致，且提交前该记录不在首位、无异常弹窗、排序码和会话一致；如同口径扫描数量存在，记录变化作辅助证据。初版成功判定须通过后台/查看所有记录抽样对账。发现结果不明、字段缺失、数量冲突或记录可能来自其他操作员，标记 UNKNOWN 并暂停。成功记录写入 SQLite 后才取下一条。

验收：新增运单显示在最新记录并与 WMS 对账；错误提示中的运单不算成功；29 总包裹数与 40 扫描数不做差；断线/超时不会重复 Enter。

### F10 重复组包与异常恢复（P0）

实现：识别精确已验证错误文本“已扫描包裹已在当前大包中”，确认弹窗/原运单/目标会话一致，提取 Logid，标记 ALREADY_IN_BAG（非新增成功）。弹窗常有两个 EditText；通过结构、焦点、enabled、visible 定位唯一目标，不写死坐标；重新输入原运单号后 Enter，等待弹窗消失、同大包和排序码、唯一空输入框；仅当 WMS 明确进入已核验的容器校验阶段才可输入同一大包号。当前实测一次重输原运单即恢复；容器校验阶段未实测，默认暂停人工处理，不能盲输大包号。一次恢复失败即暂停，不重复循环。

验收：重复包裹计数 +1、新增成功 +0；恢复后输入框空、弹窗消失且会话正确；恢复失败保留日志并暂停；其他大包归属、排序错误不得自动重新分配。

### F11 任务队列、暂停、停止与续扫（P0）

实现：单 worker 按冻结顺序执行；状态见第 5 节。暂停在当前提交达到可判定状态后生效，UI 显示 PAUSING；停止不发送新运单，不关闭大包；不得强杀 worker。紧急中止或进程崩溃留下 IN_FLIGHT，重启转换为 UNKNOWN；需通过 WMS 记录核实后人工解决，禁止自动重试。任务支持按已核验条目运行；WAIT_PHYSICAL 不自动提交。终态不因重复导入而重扫；历史记录同运单但不同任务/仓库/容器时展示并人工核对，不把历史成功当作永久全局锁。

验收：暂停后无新提交；恢复不会跳过未知结果；退出程序再打开，历史状态不丢失；任务在故障后可审计地继续。

### F12 SQLite 持久化与审计（P0）

实现：外键、事务、唯一约束；数据库放本机磁盘，WAL + busy_timeout，线程各自连接，单写入服务优先。每次状态迁移和 attempt 记录原子提交，发送前先落盘；终态/统计从持久数据聚合。使用 schema_version 和顺序迁移，迁移前备份；升级失败不自动覆盖数据库。设备指令和本地 DB 不能组成同一个原子事务，故必须保留 UNKNOWN 分支。历史操作不可无痕修改；人工解决记录操作者及理由。

验收：注入崩溃于发送前、发送后、结果落盘前，均不出现无审计重扫；唯一约束阻止同任务重复运单；备份还原通过。

### F13 桌面界面与交互（P0）

实现：主界面顶部设备和仓库状态；左侧导航（任务、配置、历史、设置）；任务页依次为文件选择、筛选阈值、渠道配置检查、候选表、实物核验、开始/暂停/继续/停止、统计和日志。QAbstractTableModel 支持大表分页/排序；QThread worker 通过 Signal 发状态变更；暂停请求使用线程安全 Event，避免长阻塞 slot 导致 queued signal 无法执行。运行中锁定配置和设备；状态驱动按钮可用性。错误详情允许复制 Logid，保留原始日志引用。

验收：扫描中 UI 不冻结；无配置/无授权/无设备/未核验时开始按钮禁用；开始、暂停、停止的状态与数据库一致；关闭窗口时提示在途提交风险。

### F14 统计、报告及日志（P0）

实现：区分新增成功、已在当前大包、待核验、待执行、在途、异常待核查、取消。任务总数 N = 各互斥状态数量之和；已确认完成 = SUCCESS + ALREADY_IN_BAG；完成率 = 已确认完成/N；处理进度可单独显示（已尝试/N，定义不混用）。历史导出 CSV/XLSX：运单、carrier、sort、container、源行、时间、结果、attempt_id、Logid、操作者、核验引用。CSV 防公式注入；XLSX 运单列文本。日志滚动、截图/XML 脱敏、访问权限限制，不记录密码、授权 token 或登录页面内容。保留期建议 30 天，需业务确认；清理须明确授权，不做批量删除。

验收：统计守恒；重复组包不增新增数；导出与数据库一致；含前导零的运单保留；日志中无凭证。

### F15 安装、诊断、升级与卸载（P0）

实现：锁定依赖和 Python；Windows x64 使用 PyInstaller onedir 构建，明确 ADB platform-tools、驱动安装/许可证处理；首次运行诊断 ADB、LibUSB、数据库写权限、设备授权、WMS 版本。签名/校验和按公司 IT 要求；升级前备份数据库，保留数据；卸载不默认删业务历史。提供 README 安装和故障排查。

验收：在干净 Windows 测试机无需手工安装 Python 即可启动；ADB 缺失、未授权、驱动异常有明确指引；升级保留旧任务；回滚有测试记录。

### F16 模拟器与开发测试工具（P1）

实现：DeviceAdapter 接口提供 FakeDeviceAdapter，读取脱敏 XML fixtures 并记录动作，模拟超时、断线、弹窗和成功结果；模拟任务与真实数据库/统计隔离，明显显示 DEMO，不能切换到真实提交。可选 scrcpy 预览仅用于观察，人工操作时先暂停任务并重新校验会话。

验收：无 PDA 可以执行导入、筛选、状态机和 UI 流程回归；模拟模式无真实 ADB 写操作。

## 5. 状态机、关键接口与执行算法

任务状态：DRAFT → READY → RUNNING → PAUSING → PAUSED → RUNNING；RUNNING 可转 BLOCKED / COMPLETED / STOPPED。只有全部候选达到已确认终态或有明确人工处置时才允许结束；若仍有待核验/未知，不能称全部成功。STOPPED 为可恢复的任务停止状态，续扫需重新授权与核验。

包裹状态：WAIT_PHYSICAL → READY → IN_FLIGHT → SUCCESS | ALREADY_IN_BAG | UNKNOWN | REJECTED；READY 可取消；UNKNOWN 只能由授权人工核查记录后转入明确状态，确认 WMS 未提交且实物仍正确才允许回 READY 并创建新 attempt。ALREADY_IN_BAG 的恢复状态另存 recovery_status，恢复失败仍阻止队列继续。

| 模块/接口 | 输入 | 输出/约束 |
|---|---|---|
| DeviceAdapter.snapshot() | serial | XmlSnapshot / typed error，只读 |
| StateDetector.detect(snapshot) | XML | PageState, bag, sort, count_label/value, latest_records, focused_fields, error, logid |
| WmsController.open_bag(config) | bag + sort | VerifiedSession，否则异常 |
| WmsController.submit(item, session) | 已核验 QueueItem | ScanResult；最多一次 Enter |
| WmsController.recover_duplicate(result, session) | 原运单 + 已验证会话 | RecoveryResult，不能新增计数 |
| TaskRepository.begin_attempt(item) | item_id | 持久 attempt_id + IN_FLIGHT |
| TaskRepository.finish_attempt() | evidence + outcome | 原子更新 item 与审计事件 |
| ImportService.build_preview() | file, mapping, rules | ImportSummary + candidate rows |

核心伪代码（实际模块需单元测试）：

```python
for item in task.frozen_order:
    if stop_requested(): break
    if pause_requested(): wait_at_safe_boundary()
    if item.status != READY: continue
    validate_physical_evidence(item)
    session = verify_or_open_expected_bag(item)
    validate_current_page(session)
    attempt = repo.begin_attempt(item)  # commit before external action
    try:
        result = controller.submit_once(item, session)
        outcome = classify_with_evidence(result)
    except DeviceError:
        outcome = UNKNOWN
    repo.finish_attempt(attempt, outcome)
    if outcome == ALREADY_IN_BAG:
        if not controller.recover_duplicate(result, session).verified:
            block_task(); break
    elif outcome != SUCCESS:
        block_task(); break
```

所有业务保护使用显式异常/条件判断，不使用可被 `python -O` 禁用的 assert。建议读状态轮询 0.3–0.5 秒、页面超时 10 秒、业务反馈超时 15 秒；均为待实测可配置参数。读操作可有限重试；任何可能已提交的写操作不可自动重试。用 monotonic 时钟处理超时。

## 6. 数据模型与配置契约

| 表 | 核心字段与约束 |
|---|---|
| imports | id PK, file_name, sha256, sheet, mapping_json, imported_at_utc, timezone, row_count |
| import_rows | id PK, import_id FK, row_no, raw_json, tracking TEXT, carrier, status_raw, outbound_utc, exclude_reason |
| carrier_configs | id PK, carrier_key, sort_code, bag_no TEXT, aliases_json, enabled, version, updated_at; UNIQUE(carrier_key, sort_code) |
| tasks | id PK, import_id FK, state, as_of_utc, threshold_minutes, warehouse, device_serial, config_snapshot_json, created_by, timestamps |
| task_items | id PK, task_id FK, tracking TEXT, carrier, sort_code, bag_no TEXT, outbound_utc, queue_index, status, evidence_id, recovery_status; UNIQUE(task_id, tracking) |
| item_sources | item_id FK, import_row_id FK; 复合 PK，保留去重来源 |
| physical_checks | id PK, item_id FK, tracking, bag_no, operator, checked_at, method, evidence_ref, revoked_at |
| attempts | id PK, item_id FK, attempt_no, session_id, started_at, finished_at, outcome, before_json, after_json, logid, error_code, recovery_status; UNIQUE(item_id, attempt_no) |
| events | id PK, task_id FK, item_id FK nullable, attempt_id FK nullable, event_type, actor, at_utc, detail_json |

所有时间存 UTC ISO 8601，界面按确认的现场时区转换。JSON 保存配置快照，不能代替关键列约束；运单与容器均 TEXT。数据库事务应保证同一任务仅一个在途提交。启动恢复扫描所有 IN_FLIGHT 和未完成恢复事件，阻止自动运行。日志截图以 attempt_id 命名，禁止在日志中存敏感凭证。

建议配置：`warehouse=[仓库ID已脱敏]`（本仓部署确认）；`locale=zh-CN`；`threshold_minutes=30`（建议）；`report_timezone=America/New_York`（需确认）；`poll_interval_ms=500`、`page_timeout_s=10`、`submit_timeout_s=15`（建议）；`real_mode=false`（默认）；`auto_close_container=false`（不可通过 UI 开启）。

## 7. 非功能需求与运行约束

- 安全：最低权限、人工 WMS 登录、访问本地目录限授权账号；不暴露 ADB 到公网，不使用未授权接口；所有真实操作必须有现场许可和实物证据。
- 可靠性：单设备串行；每个提交都有 attempt；未知结果隔离；异常恢复不得无限循环；不使用报表状态替代实体核验。
- 性能建议门槛：10,000 行 XLSX 在标准现场 PC 上 10 秒内完成预览（以实测为准）；扫描节奏以 WMS 响应及实体核验为准，不预设吞吐或绕过限流；UI 常规交互 300ms 内有反馈；不因固定 sleep 而盲目继续。
- 可维护性：依赖锁定、业务规则和标签表版本化、测试 fixture 脱敏、CI 运行单测与 lint；默认复用成熟库，禁止批量删除文件，单文件删除需明确路径与异常处理。
- WMS 更新：版本变化显示风险；先运行只读导航测试，再经批准恢复真实模式；发现标签、字段、响应语义变化即停用相关自动操作。

## 8. 开发计划与交付里程碑

以下为建议工作量（单开发人员工作日，不是承诺日历；实物核验方式和现场资源决定进度）。所有阶段以验收门槛进入下一阶段。

| 阶段 | 工期建议 | 交付物 | 退出条件 |
|---|---|---|---|
| M0 已完成 | 已完成 | PDA 实测日志 | V01–V07 证据已记录；不是生产签字 |
| M1 | 1–2 日 | 工程骨架、依赖锁、FakeDevice、SQLite schema | 可启动、单测跑通、数据库迁移通过 |
| M2 | 2–3 日 | F01/F02/F07/F08/F09/F10 控制器 | XML fixture 回归、单条授权真机验证 |
| M3 | 2–3 日 | F03–F06 导入与配置、实体核验 | 报表字段/时区签字；筛选和去重全通过 |
| M4 | 2–3 日 | F11/F12 队列、事务和恢复 | 故障注入无重复提交；人工对账可用 |
| M5 | 2–3 日 | F13/F14 UI、统计与导出 | 操作员完成模拟流程；账表一致 |
| M6 | 2–4 日 | F15 打包、现场测试与培训 | 小规模试点、对账、安装升级验收 |

合计建议 11–18 个开发工作日，现场排期及审批另计。开发负责人负责代码及测试，QC 负责数据规则与对账，现场主管负责实物流程及 UAT，IT 负责 Windows/PDA 权限与部署。用户未指定人员姓名，角色为建议分工。

## 9. 测试与验收矩阵

测试数据优先离线 fixture；真实 WMS 仅使用现场授权、明确实物归属的测试包裹，禁止为了测试制造错误生产记录。P0 用例全部通过才可上线。

| ID / 功能 | 操作或输入 | 预期结果 |
|---|---|---|
| T01 F01 | 正常设备、unauthorized、offline、双设备 | 正确提示；未授权不提交；双设备须选择 |
| T02 F02 | 未登录、正确/错误仓库、页面缓慢 | 手动登录；错误仓库禁止运行；状态等待有超时 |
| T03 F03 | 正常 XLSX、缺列、损坏、公式无缓存、长数字 | 正确预览或拒绝；原文件不变、无运单精度损失 |
| T04 F04 | 状态复核完成/其他；阈值 29:59/30:00；未来时间 | 仅正确候选入队；歧义时间待核查 |
| T05 F04 | 同渠道重复、跨渠道重复、状态冲突 | 合并同类；冲突隔离；源行可追溯 |
| T06 F05 | 缺配置、多 sort、错误大包或 sort | 阻止运行；不得自动猜测 |
| T07 F06 | 无实物证据、实体标签不符、已核验 | 前两者零提交；正确者允许入队 |
| T08 F07 | XPath stale、软键盘、两输入框、未知页面 | 重新读取；无盲点；未知暂停 |
| T09 F08 | 承运商A→承运商B→承运商A；back 先收起键盘 | 正确验证各容器；关闭容器动作 0 |
| T10 F09 | 授权新增包裹；错误文本也含运单 | 成功以最新记录及对账为证；错误不算成功 |
| T11 F10 | 当前大包重复组包；重输原运单 | ALREADY_IN_BAG；恢复后继续；新增 +0 |
| T12 F10 | 其他大包/未知错误/恢复失败 | 暂停；不移包、不重复提交，保存证据 |
| T13 F11 | 扫描中暂停/停止，随后继续 | 当前结果核实后停；不发送后续；恢复不漏不重 |
| T14 F11/F12 | Enter 前/后断线、进程崩溃、数据库写失败 | UNKNOWN 或阻止提交；人工对账；无自动重试 |
| T15 F12 | 同报表重复导入、历史成功、人工纠正 | 历史提示；任务内唯一；人工处置可追踪 |
| T16 F13 | 模拟 10,000 行、扫描中交互 | 预览性能达标或记录偏差；UI 不冻结 |
| T17 F14 | 成功/重复/未知/待核验混合 | 各状态加总等于候选；导出对账一致 |
| T18 F15 | 干净 Windows 安装、升级、回滚 | 可运行；缺依赖提示；数据库与历史保留 |
| T19 F16 | FakeDevice 模拟所有路径 | 零真实写调用；所有核心状态可回归 |
| T20 F08–F14 | 小批量连续、多渠道、断线/恢复 | 每条可对账；错误归属 0；不明提交未解决前停机 |

验收证据：测试编号、软件版本/commit、PDA/WMS 版本、输入文件哈希、设备 serial、执行人、时间、实际输出、SQLite 导出、脱敏 WMS 截图/Logid、通过/失败与缺陷编号。实际测试记录另填，本文不伪造通过结果。

## 10. 上线门槛、试点与回滚

建议分级：离线模拟全部通过 → 授权单条成功/重复 → 10 条跨渠道小批 → 50–100 条连续扫描 → 一个实际受控班次试点。数量为建议上限/阶段目标，需现场批准，不强行制造包裹。每阶段先与 WMS 记录逐条核对，再放大范围。

硬性上线门槛：P0 用例全部通过；错误容器/排序码提交 0、未经实物核验提交 0、自动关闭容器 0、重复新增统计 0、未解决的 UNKNOWN 0、测试包裹 WMS 与本地记录对账一致率 100%；无阻断/高严重缺陷；IT/QC/现场主管批准。性能指标如未达建议值必须记录并经批准，不能降低正确性要求。

回滚：停止 worker → 核实在途项 → 导出任务和证据 → 人工接管 PDA → 恢复上一已批准软件版本和兼容数据库备份。已在 WMS 提交的记录不能通过本地回滚撤销；如有实际误操作，走公司授权业务纠错流程，不自动反向操作或改库存。

验收签署：开发负责人 ______ 日期 ______；QC ______ 日期 ______；现场主管 ______ 日期 ______；IT ______ 日期 ______。结论：通过 / 有条件通过（不得豁免安全门槛）/ 不通过。

## 11. 开发启动清单与待确认问题

1. 在现有项目建立 Git 分支和工程骨架，读取已有 AGENTS.md（如存在），遵守其代码规则；不得覆盖现有文件或批量删除。
2. 提供一份脱敏真实出库包裹报表，确认表头、日期时区和数据类型；建立 fixture。
3. 确认快递渠道与排序码全集、实物核验操作方式、授权用户和操作区域。
4. 实现 DeviceAdapter、StateDetector 与 WmsController；先离线回归再运行批准的真实操作。
5. 实现队列和 SQLite 事务，再接入 PySide6，避免先做界面后补可靠性。
6. 确认 WMS 扫描数量口径及后台对账入口、日志留存期、目标 Windows 版本、驱动部署权限、实际吞吐目标。

## 12. 技术参考

以下为库的公开技术文档，用于实现和查阅；WMS 行为结论以本次实测为准。

- uiautomator2 项目：https://github.com/openatx/uiautomator2
- XPath 扩展：https://github.com/openatx/uiautomator2/blob/master/XPATH.md
- Qt for Python QThread：https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html
- openpyxl 读取接口：https://openpyxl.readthedocs.io/en/stable/api/openpyxl.reader.excel.html
- SQLite WAL：https://sqlite.org/wal.html
- PyInstaller 文档：https://pyinstaller.org/en/stable/
