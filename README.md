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

 
