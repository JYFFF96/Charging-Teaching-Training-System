# 一体化客户API（8088）

客户服务由ykc_platform_server.py启动，共享实时设备会话。管理接口仍在8000，客户会话不能访问管理权限。页面及API同源，POST使用JSON、X-Customer-Request: 1及Origin；会话HttpOnly/SameSite Cookie。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/customer/config | HTTP模式、设备模式、测试额度（元） |
| GET | /api/customer/me | 当前客户 |
| POST | /api/customer/register | username,password,security_question_id,security_answer |
| POST | /api/customer/login | username,password |
| POST | /api/customer/logout | 注销当前会话 |
| POST | /api/customer/recovery/verify | username,security_question_id,security_answer，返回5分钟一次性reset_token |
| POST | /api/customer/recovery/reset | reset_token,new_password，撤销所有旧会话 |
| GET | /api/customer/device?pile_no=...&gun_no=... | 可用性/本人订单/实时数据/计费模型 |
| POST | /api/customer/orders | pile_no,gun_no；确认并启动；同账号同枪进行中订单返回原订单，不重复发命令 |
| GET | /api/customer/orders | 本人最近100笔订单 |
| GET | /api/customer/orders/{id} | 本人订单及匹配的最新设备数据 |
| POST | /api/customer/orders/{id}/stop | 停止本人订单，须最新充电数据与订单流水匹配 |

availability: idle、mine、occupied、offline、fault、unknown。其他账号占用只返回占用提示，不返回用户、流水、金额或订单。
订单状态: starting、charging、stopping、unknown、ending、finished、failed。starting/charging/stopping/unknown/ending均占用枪；只有明确启机失败或匹配交易上报释放。服务重启将starting/stopping改为unknown，不自动重新下发。实时充电报文须匹配流水、桩和枪才能更新本人订单；空闲报文不立即释放占用，等待最终交易记录。

单位: 实时energy/amount及最终total_energy/total_amount按报文值除10000，charge_time分钟。计费模型尖峰平谷电费和服务费除100000（元/kWh）。测试启动额度来自后台参数，单位分，与用户账户余额/充值无关，不接受前端余额输入。

账号4–32位字母数字下划线，大小写不敏感；密码8–128字符。问题ID secret、character、memory。答案NFKC、去两端空白、大小写归一化，密码与答案独立加盐PBKDF2-SHA256。会话24小时，重置凭证5分钟且一次性。数据库包含原版账号表及新增orders表，保持旧账号与会话。

错误码401未登录/验证失败、404订单不存在（含非本人订单）、409占用/设备状态不允许、429请求过多。订单操作每个账号最多30次/10分钟。启动最长等待80秒，停止20秒；客户端请求超时不代表命令失败，应重新查询订单，不能重复自动启动。

支付尚未接入。客户接口不可用于直接授权扣款或余额校验。
