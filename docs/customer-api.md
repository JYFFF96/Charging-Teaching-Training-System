# 客户接口

页面与接口由同一个8088服务提供，浏览器使用HttpOnly会话Cookie，POST携带 `Content-Type: application/json`、`X-Customer-Request: 1` 和浏览器自动设置的同源Origin。正式访问使用HTTPS；不支持跨域接口调用。

| 方法 | 路径 | 状态与内容 |
|---|---|---|
| GET | /api/customer/config | 已实现，返回HTTP测试模式和充电演示标志 |
| GET | /api/customer/me | 已实现，返回当前用户；未登录401 |
| POST | /api/customer/register | 已实现，username,password,security_question_id,security_answer；创建并登录 |
| POST | /api/customer/login | 已实现，username,password；登录并设置Cookie |
| POST | /api/customer/logout | 已实现，注销当前会话 |
| POST | /api/customer/recovery/verify | 已实现，username,security_question_id,security_answer；返回reset_token |
| POST | /api/customer/recovery/reset | 已实现，reset_token,new_password；重置并注销所有旧会话 |

账号4–32位字母数字下划线，大小写不敏感；密码8–128字符且区分大小写。问题ID为secret、character、memory。答案进行Unicode NFKC、两端去空格、大小写归一化；答案归一化后2–128字符。找回时由客户选择注册时的问题，后端不提供账号是否存在或该账号的问题查询。验证错误统一返回账号/问题/答案不匹配。

单来源IP最多60次账号请求/10分钟；登录、恢复验证按账号及IP最多5次/10分钟，注册最多10次。超限429。登录成功清除该账号登录计数。重置凭证5分钟、一次性；会话24小时，存数据库可跨重启恢复。密码与答案不返回、不记录日志。

## 尚未实现

真实设备及订单接口、支付、用户与充电流水归属。充电演示仅存在当前页面内存，刷新清空，不会触发设备。后续需要后台验证费用、余额、订单归属、设备状态及幂等，并处理设备启停超时结果未知。客户不得调用原版管理接口。
