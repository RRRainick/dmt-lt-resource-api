# 接口功能测试1

## 说明

- 修改node为对端bmv2 IP 和端口(8021). 
- 测试时HTTP Server不区分`node_id`.

## 运行步骤

```bash
# 模拟的agent不用启动
# 启动manager
cd /path/to/resource-api
DECISION_ENABLED=false python3 -m resource_manager.run

# 查看结果
cd /path/to/resource-manager
watch -n 1 'tail -n 20 mock_influxdb.json'
```

# 接口功能测试2

## 运行步骤

```bash
# 终端1
cd /path/to/resource-api
python3 -m resource_manager.mock_decision_server_once

# 终端2

cd /path/to/resource-api
LOG_LEVEL=DEBUG python3 -m resource_manager.run
```
