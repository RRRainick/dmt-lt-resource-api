### 启动 resource manager 的步骤

#### Ubuntu 前置依赖
- Python 3.10
- pip
- `jq`（可选，仅用于格式化查看 `mock_influxdb.json`）

安装jq
```bash
sudo apt-get update && sudo apt-get install -y jq
python3 -m pip install -r resource_manager/requirements.txt
```

#### 启动 依赖

config.json 的内容替换为 node_id 和 ip 地址

```json
    {
      "node_id": "IPL238",
      "base_url": "http://192.168.104.238:8000",
      "enabled": true
    }
```

#### 说明：

- `database.enabled=false`：从 node 拿到资源数据后写入根目录的 `mock_influxdb.json`。因为只是和课题2对接口，所以数据库用文件形式替代。
- `database.enabled=true`：使用 `influxdb.InfluxDBClient` 写入配置中的真实 InfluxDB。
- `mock_decision_server` 用于模拟决策子系统接入点。


#### 接口功能测试1：验证资源读取和存入数据库
##### 命令

step1 启动节点上的资源上报和资源配置程序。`config.json` 中只有 `enabled=true` 的节点会被监测和配置；例如 `node_id = sdn234` 的 `base_url = http://192.168.104.234:8000`。

这里终端1 启动资源代理程序（这里是课题三提供的一个测试脚本）：
```bash
cd /path/to/resource-api
python3 -m resource_manager.agent
```

step2 终端2 启动资源管理器并只验证资源读取和数据库文件写入：
```bash
cd /path/to/resource-api
DECISION_ENABLED=false python3 -m resource_manager.run
```


##### 结果查看
验证结果通过查看 mock_influxdb.json
```bash
cd /path/to/resource-manager
watch -n 1 'tail -n 20 mock_influxdb.json'

#显示如下成功
"node:IPL238@timestamp:1784885538411": {
      "time": 1784885538411,
      "node_id": "IPL238",
      "cpu_ratio": 0.009790832220738763,
      "mem_max": 67015.192576,
      "mem_util_ratio": 7460.442112,
      "trans_max": 10000.0,
      "trans_util_ratio": 0.0,
      "mode_resource_list": "{\"geo\":{\"cpu_ratio\":0.000148,\"mem_util_ratio\":9.46176,\"trans_util_ratio\":0.0},\"ipv4\":{\"cpu_ratio\":0.000137,\"mem_util_ratio\":9.703424,\"trans_util_ratio\":0.0},\"ipv6\":{\"cpu_ratio\":0.000148,\"mem_util_ratio\":9.797632,\"trans_util_ratio\":0.0},\"ndn\":{\"cpu_ratio\":0.000144,\"mem_util_ratio\":9.883648,\"trans_util_ratio\":0.0},\"srv6\":{\"cpu_ratio\":0.000149,\"mem_util_ratio\":10.043392,\"trans_util_ratio\":0.0}}"
    },
```
目前文件夹里的 mock_influxdb.json 是保存的测量数据


#### 接口功能测试2：验证资源配置
##### 命令

step1 终端1 启动资源代理程序（这里是课题三提供的一个测试脚本）：
```bash
cd /path/to/resource-api
python3 -m resource_manager.agent
```

step2 终端2 启动模拟的决策子系统：

```bash
cd /path/to/resource-api
python3 -m resource_manager.mock_decision_server_once

#显示如下成功
[mock-decision] running at http://0.0.0.0:9000; waiting for manager workflow...
[mock-decision] 1/4 收到策略请求：request_id=4
[mock-decision] 1/4 返回资源配置策略：nodes=IPL238
[mock-decision] 4/4 收到资源配置结果：request_id=4 status=success success=1 failed=0
[mock-decision] 4/4 已确认收到资源配置结果：request_id=4
[mock-decision] 单次资源配置流程结束
```

step3 终端3 启动资源管理器：

```bash
cd /path/to/resource-api
LOG_LEVEL=DEBUG python3 -m resource_manager.run

#显示如下成功
LOG_LEVEL=DEBUG python3 -m resource_manager.run
DEBUG Using selector: EpollSelector
INFO:     Started server process [1824894]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:9001 (Press CTRL+C to quit)

DEBUG 1/4 收到课题四资源配置策略：request_id=1 nodes=IPL238
DEBUG 2/4 下发资源配置：node_id=IPL238 request_id=1 ipv4={"compute_config_percent": 50.0, "storage_config_mb": 33507.596288, "forwarding_config_mbps": 5000.0}
DEBUG 3/4 收到 node 响应：node_id=IPL238 request_id=1 code=0 msg=ok data={"request_id": 1}
DEBUG 4/4 回传资源配置结果：request_id=1 success=1 failed=0
```

资源配置模式仍会同时执行资源读取和存储。

 

## 模态操作工具

`modality_cli.py` 每次执行一次 `POST /modality/deploy` 或
`POST /modality/delete`，部署后保留模态，不自动配置资源或清理。
需要 Python 3.10+，仅使用标准库。连接参数必须显式传入。

```bash
python3 modality_cli.py \
  --ip 192.168.134.178 --port 8021 --node-id IPL238 \
  --action deploy --modality api-test-mode \
  --compute-config-percent 10 \
  --storage-config-mb 128 \
  --forwarding-config-mbps 10

python3 modality_cli.py \
  --ip 192.168.134.178 --port 8021 --node-id IPL238 \
  --action delete --modality api-test-mode
```

`--modality` 为非空字符串，去除首尾空白并保留大小写。
部署资源参数默认分别为 10%、128 MB、10 Mbps；计算配额范围为 0–100，
存储和转发配额必须是非负有限数值。`delete` 不接受资源配额参数。
`--scheme` 默认为 `http`，支持 `https`；`--timeout` 默认为 3 秒。

标准输出为服务端返回的 JSON，错误信息写入标准错误。
HTTP 2xx 且响应对象的整数 `code` 为 0 时退出码为 0；
请求失败、非法响应或业务失败为 1，参数错误为 2。不自动重试请求。

### Python 库调用

```python
from modality_client import ModalityClient

client = ModalityClient("http://192.168.134.178:8021", timeout=10)
result = client.deploy(
    "IPL238", "api-test-mode",
    compute_config_percent=10,
    storage_config_mb=128,
    forwarding_config_mbps=10,
)
print(result.status, result.payload)
# 需要删除时单独调用：
# result = client.delete("IPL238", "api-test-mode")
```

库自动生成时间戳和请求 ID，也可通过 `request_id=123` 指定请求 ID。
`deploy()` 和 `delete()` 返回 `HttpResult`，包含 HTTP 状态、正文、JSON、
JSON 解析错误和耗时。HTTP 或业务失败仍返回结果，由调用方判断；
无法连接或读取响应时抛出 `TransportFailure`。
库的请求体构造函数 `deploy_body()`、`delete_body()` 和底层 `request()`
保留直接构造异常请求的能力，不执行 CLI 参数校验。

`api_test.py` 复用同一库并保留原来的严格响应断言、预检及清理逻辑。
复制工具或测试脚本到其他主机时，需同时复制 `modality_client.py`。

本地回归测试（仅访问本机模拟 HTTP 服务）：

```bash
python3 -m unittest -v test_modality
```
