# GS1 条码元素串 / Digital Link 解析 API

基于 Python + FastAPI 的 GS1 解析服务。`/parse` 接口同时接受两种输入：

1. **元素串（Element String）**：扫码枪原始输出（含 ASCII 29 / FNC1 分隔符）；
2. **GS1 Digital Link URI**：以 `http://` 或 `https://` 开头的链接，AI/值对位于路径
   （`/AI/值/AI/值`）和查询参数（`?AI=值&AI=值`）中。

两种输入都返回保持原顺序的结构化元素，并完成同一套字符集、长度、GTIN Mod-10
校验位与真实日期校验。重量 AI（310n/320n）在两种输入下行为完全一致，响应除
原始 `value` 外还会返回 `decimal_value`（按隐含小数位解释后的数值）与 `unit`。

## 一键启动

```bash
docker compose up --build
```

需要后台运行时，一条命令即可（守护态启动、自动构建）：

```bash
docker compose up --build -d
```

启动后通过 **8080** 端口访问：

- API 地址：http://localhost:8080
- 交互式文档（Swagger UI）：http://localhost:8080/docs
- 健康检查：http://localhost:8080/health

查看日志与停止服务：

```bash
docker compose logs -f   # 查看日志
docker compose down      # 停止
```

## 支持的 AI

| AI   | 含义        | 长度            | 字符集                          |
| ---- | ----------- | --------------- | ------------------------------- |
| 01   | GTIN        | 固定 14 位数字  | 0-9，含 Mod-10 校验位验证       |
| 17   | 效期 YYMMDD | 固定 6 位数字   | 0-9，校验真实日期（含闰年）     |
| 10   | 批号        | 可变 1-20 字符  | GS1 AI 字符集 82                |
| 21   | 序列号      | 可变 1-20 字符  | GS1 AI 字符集 82                |
| 310n | 净重（千克 kg） | AI 4 位 + 固定 6 位数字 | 0-9；`n` 为 0-9 的隐含小数位数 |
| 320n | 净重（磅 lb）   | AI 4 位 + 固定 6 位数字 | 0-9；`n` 为 0-9 的隐含小数位数 |

重量 AI 说明：

- 标识符共 4 位：前缀 `310`（千克）或 `320`（磅）+ 1 位小数位指示符 `n`
  （0-9），其后**固定 6 位数字**，按长度自动定界（与 01/17 一样无需分隔符）。
- 解析器按**最长可匹配**识别四位标识符：`310n`/`320n` 优先于两位 AI；
  例如 `3103...` 识别为 AI `3103`，而 `3110...` 仍按两位 AI `31` 处理
  （`31` 不受支持，返回 `UNSUPPORTED_AI`）。
- 值按整数除以 10^n 解释：`3103 002513` → `decimal_value = 2.513`，
  `unit = "kg"`；`3202 002513` → `25.13 lb`。`n = 0` 时为整数；
  `n > 6` 时高位补隐含的零，如 `3109 000001` → `0.000000001 kg`。
- 响应中的 `value` 保留**原始 6 位数字**（不插小数点），仅重量元素额外携带
  `decimal_value`（JSON number）与 `unit`（`"kg"` / `"lb"`）两个字段。

解析规则：

- 固定长度字段（01、17、310n、320n）按长度自动定界，无需分隔符。
- 可变长度字段（10、21）除位于串末尾外，必须以分隔符 ASCII 29（GS，扫码枪 FNC1）
  结束；JSON 中以 `\u001d` 表示。
- AI 17 的日字段为 `00` 时按 GS1 规范表示当月最后一天；年份按 20YY 解释。
- `data` 以 `http://` 或 `https://` 开头时按 GS1 Digital Link URI 解析，否则按
  元素串解析。

### Digital Link URI 规则

- 路径段必须成对出现：`/AI/值/AI/值...`；查询参数按 `AI=值&AI=值` 解析（参数没有
  等号时值为空，会触发 `FIELD_EMPTY`）。
- 值先做百分号 UTF-8 解码（`%XX`），再执行与元素串完全相同的字符集、长度、校验位与
  日期校验；`offset` 始终指向**原始 URI** 中的字节位置（解码后字符报错时定位到它的
  首个编码字节，即 `%` 所在字节）。
- 未知 AI、路径内重复 AI、查询参数内重复 AI，以及同一 AI 同时出现在路径和查询参数
  中，均返回 `422`。
- 响应中路径提取到的元素按路径顺序在前，查询参数提取到的元素按参数顺序在后。
- 片段（`#...`）不属于 Digital Link 数据，解析时忽略。

## 使用示例

### 元素串

请求（注意批号与序列号之间的 `\u001d` 分隔符）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "01095011015300031725013110ABC123\u001d21SN0001"}'
```

响应 `200 OK`，元素按原始顺序返回，`offset` 为 AI 在输入中的字节偏移：

```json
{
  "elements": [
    { "ai": "01", "name": "GTIN",     "value": "09501101530003", "offset": 0  },
    { "ai": "17", "name": "EXPIRY",   "value": "250131",         "offset": 16 },
    { "ai": "10", "name": "BATCH/LOT","value": "ABC123",         "offset": 24 },
    { "ai": "21", "name": "SERIAL",   "value": "SN0001",         "offset": 33 }
  ]
}
```

### GS1 Digital Link URI

路径和查询参数混用的请求（路径中的元素在前，查询参数中的元素在后）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/01/09501101530003/17/250131?10=ABC123&21=SN0001"}'
```

响应与元素串完全一致，`offset` 是 AI 在**原 URI** 中的字节偏移：

```json
{
  "elements": [
    { "ai": "01", "name": "GTIN",      "value": "09501101530003", "offset": 19 },
    { "ai": "17", "name": "EXPIRY",    "value": "250131",         "offset": 37 },
    { "ai": "10", "name": "BATCH/LOT", "value": "ABC123",         "offset": 47 },
    { "ai": "21", "name": "SERIAL",    "value": "SN0001",         "offset": 57 }
  ]
}
```

值可使用百分号编码，按 UTF-8 解码后再做同一套校验。下面的批号解码后为
`Lot A/100`（百分号编码指向原 URI 字节位置，示例中 `/` 编码为 `%2F`；
注意空格不属于字符集 82，仅作编码演示）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/10/Lot%20A%2F100"}'
```

该请求会返回 `422 INVALID_CHARACTER`，`offset` 指向编码空格的 `%20` 首字节。
仅含合法字符的示例：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/10/Lot%2FA%2F100"}'
```

解码后批号为 `Lot/A/100`，返回 `200`。

路径段必须成对，例如 `.../01/09501101530003/17` 会返回 `422`
（`UNPAIRED_PATH`）；同一 AI 不能重复，也不能同时出现在路径和查询参数中
（`DUPLICATE_AI`）。

### 重量 AI（310n / 320n）

元素串与 Digital Link 两种输入返回完全相同的重量字段。元素串示例（`n=3`，
`002513` 表示 2.513 kg）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "01095011015300033103002513"}'
```

Digital Link 示例（路径中的千克重量，查询参数中的磅重量）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/01/09501101530003/3103/002513?3202=002513"}'
```

两种请求的重量元素结构一致，`value` 为原始 6 位数字，`decimal_value` 为按
`n` 位隐含小数点解释后的数值，`unit` 为单位：

```json
{
  "ai": "3103",
  "name": "NET WEIGHT (KG)",
  "value": "002513",
  "decimal_value": 2.513,
  "unit": "kg",
  "offset": 16
}
```

```json
{
  "ai": "3202",
  "name": "NET WEIGHT (LB)",
  "value": "002513",
  "decimal_value": 25.13,
  "unit": "lb",
  "offset": 49
}
```

非重量元素（01/10/17/21）不会出现 `decimal_value` 与 `unit` 字段。

## 批量重量汇总 `POST /weight-summary`

一次提交多条扫码字符串，按 **GTIN（AI 01）+ 批号（AI 10）** 分组汇总净重。

- 请求体：`{"scans": ["...", ...], "target_unit": "kg"}`。`target_unit`
  可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`scans` 中的每一项既可以是
  元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**零个或一个 AI 10**（批号可缺省）和
  **恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；可有可无的 AI 17/21
  会被忽略。缺少/重复 AI 01、重复 AI 10、缺少或出现多个重量 AI，或任一条
  扫描解析失败，整个请求返回 `422`，`detail.scan_index` 给出出错下标
  （从 0 开始），`detail.error` 为该条扫描的原始错误（结构同 `/parse`）。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建（`整数 / 10^n`），
  不使用解析结果中的 float `decimal_value`，累加也全程 `Decimal`，禁止
  float 累加。换算系数固定为 **1 lb = 0.45359237 kg**（磅→千克相乘，
  千克→磅按该系数的精确倒数相除）。
- 分组按首次出现顺序返回；无批号（缺省 AI 10）的扫描归入 `batch: null`
  的独立分组。每组包含 `gtin`、`batch`、`count`、`unit` 与 `total`：
  `total` 是组内所有重量转换到 `target_unit` 后之和，最终按**六位小数
  half-even（银行家舍入）**输出为 JSON 数字（如 `2.513000`，恒为六位，
  即使为零也显示 `0.000000`）；舍入在分组累加完成后一次性进行。
- `scans` 为空数组时返回 `200` 与空的 `groups`。

请求示例（同一 GTIN+批号下一条千克、一条磅，目标单位 kg）：

```bash
curl -X POST http://localhost:8080/weight-summary \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "scans": ["010950110153000310B1\u001d3103002513", "010950110153000310B1\u001d3202002513", "010950110153000310B2\u001d3106123456"]}'
```

`2.513 kg + 25.13 lb × 0.45359237 = 2.513 + 11.3987762581 = 13.9117762581`，
六位 half-even 舍入为 `13.911776`；响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "groups": [
    {"gtin": "09501101530003", "batch": "B1", "count": 2, "unit": "kg", "total": 13.911776},
    {"gtin": "09501101530003", "batch": "B2", "count": 1, "unit": "kg", "total": 0.123456}
  ]
}
```

目标单位为磅时（`1 kg = 1 / 0.45359237 ≈ 2.20462262 lb`）：

```bash
curl -X POST http://localhost:8080/weight-summary \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "lb", "scans": ["01095011015300033100000001"]}'
```

```json
{"target_unit": "lb", "groups": [{"gtin": "09501101530003", "batch": null, "count": 1, "unit": "lb", "total": 2.204623}]}
```

出错时（下例下标 1 的 GTIN 校验位错误）返回 `422`：

```json
{
  "detail": {
    "scan_index": 1,
    "error": {
      "error": "INVALID_CHECK_DIGIT",
      "message": "GTIN check digit mismatch: expected 3, found 4",
      "offset": 15,
      "expected": "Mod-10 check digit 3",
      "found": "4"
    }
  }
}
```

结构类错误（缺少/重复 AI）使用同样的 `detail` 结构，错误码为 `MISSING_AI`
或 `DUPLICATE_AI`，例如重复的重量 AI：

```json
{
  "detail": {
    "scan_index": 0,
    "error": {
      "error": "DUPLICATE_AI",
      "message": "scan 0: 2 weight elements (3103, 3202); exactly one 310n/320n is required",
      "offset": 26,
      "expected": "exactly one weight AI 310n (kg) or 320n (lb)",
      "found": "3103, 3202"
    }
  }
}
```

## 按效期拣货计划 `POST /weight-pick-plan`

一次提交多条扫码字符串与一组需求，按 **GTIN + 批号 + 效期** 汇总可用重量后，
以 **FEFO（先到期先出）** 顺序为每个需求分配库存。服务地址同为
**http://localhost:8080**，接口路径为 `POST /weight-pick-plan`。

- 请求体：`{"scans": ["...", ...], "target_unit": "kg", "demands": [{"gtin": "...",
  "quantity": 5.0}, ...]}`。`target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认
  `"kg"`；`scans` 中的每一项既可以是元素串，也可以是 GS1 Digital Link URI
  （与 `/parse` 完全一致）；`demands` 每项包含 `gtin`（GTIN-14，校验 Mod-10
  校验位）与 `quantity`（目标单位下的需求重量，必须大于 0）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 17**、**零个或一个 AI 10**
  （批号可缺省）和**恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；可有可
  无的 AI 21 会被忽略。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回
  `422`，`detail.scan_index` 给出出错下标（从 0 开始），`detail.error` 为该条
  扫描的原始错误（结构同 `/parse`）。
- 需求校验：**同一 GTIN 不允许出现在两个需求中**，否则返回 `422`，
  `detail.demand_index` 给出出错的需求下标，错误码为 `DUPLICATE_GTIN`；需求
  GTIN 本身非法（长度、字符集、校验位）同样按 `demand_index` 返回 `422`。
- 汇总与分配：先按 **GTIN + 批号 + 效期** 汇总可用重量，再按 **AI 17 实际日期
  升序**执行 FEFO（日字段 `00` 按 GS1 规范视为当月最后一天）；同一日到期的批次
  保持**首次出现顺序**；每个需求的**最后一批允许部分分配**。
- 换算、汇总、比较与分配全程使用 `Decimal` 与固定换算系数
  （**1 lb = 0.45359237 kg**，与 `/weight-summary` 相同），仅在响应时按
  **六位小数 half-even（银行家舍入）** 输出 JSON 数字。
- `scans` 为空数组时返回 `200`，每个需求 `fulfilled` 为 `0.000000`、
  `shortage` 等于全部需求量、`allocations` 为空。
- 响应按需求顺序逐项输出：`gtin`、`quantity`（需求回显）、`fulfilled`
  （已分配总量）、`shortage`（缺口 = quantity − fulfilled）与 `allocations`
  （按拣货顺序列出 `{batch, expiry, quantity}`；`expiry` 为原始 AI 17 值
  YYMMDD，无批号时 `batch` 为 `null`）。

请求示例（三条扫描：B1 效期 1 月 31 日、B2 效期 2 月 28 日、B3 效期日字段
`00` 即 2 月 28 日且为磅重；需求 12.5 kg）：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "scans": ["01095011015300031725022810B23103004000", "01095011015300031725013110B13103002513", "01095011015300031725020010B33202002513"], "demands": [{"gtin": "09501101530003", "quantity": 12.5}]}'
```

FEFO 顺序为 B1（01-31）→ B2（02-28）→ B3（`00` 视为 02-28，与 B2 同日，按
首次出现顺序排在 B2 之后）；B3 的 25.13 lb 换算为 11.3987762581 kg，只分配
其中 5.987 kg。响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 12.500000,
      "fulfilled": 12.500000,
      "shortage": 0.000000,
      "allocations": [
        {"batch": "B1", "expiry": "250131", "quantity": 2.513000},
        {"batch": "B2", "expiry": "250228", "quantity": 4.000000},
        {"batch": "B3", "expiry": "250200", "quantity": 5.987000}
      ]
    }
  ]
}
```

库存不足时缺口体现在 `shortage`（下例仅有 4 kg 库存，需求 10 kg）：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"scans": ["01095011015300031725013110B13103004000"], "demands": [{"gtin": "09501101530003", "quantity": 10}]}'
```

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 10.000000,
      "fulfilled": 4.000000,
      "shortage": 6.000000,
      "allocations": [
        {"batch": "B1", "expiry": "250131", "quantity": 4.000000}
      ]
    }
  ]
}
```

需求出错时返回 `422`，`detail.demand_index` 指出出错的需求下标，例如重复的
需求 GTIN：

```json
{
  "detail": {
    "demand_index": 1,
    "error": {
      "error": "DUPLICATE_GTIN",
      "message": "demand 1: GTIN '09501101530003' is requested by more than one demand; merge the quantities into a single demand",
      "expected": "each demand GTIN to appear at most once",
      "found": "09501101530003"
    }
  }
}
```

## 错误响应

解析或校验失败返回 `422 Unprocessable Entity`，`detail` 中包含错误码、
字节偏移（`offset`，UTF-8 字节序）与预期规则（`expected`）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "0109501101530004"}'
```

```json
{
  "detail": {
    "error": "INVALID_CHECK_DIGIT",
    "message": "GTIN check digit mismatch: expected 3, found 4",
    "offset": 15,
    "expected": "Mod-10 check digit 3",
    "found": "4"
  }
}
```

错误码一览：

| 错误码                 | 说明                                                          |
| ---------------------- | ------------------------------------------------------------- |
| `EMPTY_INPUT`          | 输入为空（元素串为空，或 URI 路径/查询中没有任何 AI/值对）    |
| `UNEXPECTED_SEPARATOR` | 在应出现 AI 的位置遇到分隔符（仅元素串）                      |
| `INVALID_AI`           | 不是合法的数字 AI（元素串为非 2 位数字；URI 为非数字段/键）   |
| `UNSUPPORTED_AI`       | 不支持的 AI（仅支持 01/10/17/21 及 310n/320n）                |
| `INVALID_PRECISION`    | 310/320 前缀后缺少或给出非数字的精度位 `n`（须为 0-9）        |
| `FIELD_TOO_SHORT`      | 固定长度字段字符不足（含重量字段不足 6 位数字）               |
| `FIELD_EMPTY`          | 可变长度字段为空                                              |
| `FIELD_TOO_LONG`       | 可变长度字段超过 20 字符（或固定长度字段超长，仅 URI）        |
| `INVALID_CHARACTER`    | 字符不符合该字段字符集                                        |
| `INVALID_CHECK_DIGIT`  | GTIN Mod-10 校验位不匹配                                      |
| `INVALID_DATE`         | 效期不是真实日期                                              |
| `UNPAIRED_PATH`        | Digital Link URI 路径段不是成对的 AI/值（仅 URI）             |
| `DUPLICATE_AI`         | AI 重复出现，或同时出现在路径与查询参数中（仅 URI）           |
| `INVALID_PERCENT_ENCODING` | `%XX` 不是合法十六进制转义或不是合法 UTF-8（仅 URI）      |

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                 # 运行测试
uvicorn app.main:app --reload --port 8080
```

## 项目结构

```
app/
  main.py     # FastAPI 应用与请求/响应模型（/parse、/weight-summary、/weight-pick-plan）
  gs1.py      # GS1 元素串 / Digital Link URI 解析与校验核心
  summary.py  # 多扫描重量汇总：Decimal 重建、单位换算、GTIN+批号分组
  pickplan.py # FEFO 拣货计划：GTIN+批号+效期汇总、按效期分配需求
tests/
  test_parse.py
  test_weight_summary.py
  test_weight_pick_plan.py
Dockerfile
docker-compose.yml
requirements.txt      # 运行依赖
requirements-dev.txt  # 测试依赖
```
