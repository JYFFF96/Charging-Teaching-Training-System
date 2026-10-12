#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# 云快充平台协议 V1.6 —— 平台端后台服务（模拟运营平台）
#
# 功能：
#   1. TCP 长连接服务器，支持多充电桩并发接入（线程模型）
#   2. 完整帧封装/解析：0x68 起始符、长度域、序列号、CRC16(附录13.2)
#   3. 接收并解析全部充电桩上行报文（0x01/0x03/0x05/0x09/0x13/0x15/
#      0x17/0x19/0x1B/0x1D/0x21/0x23/0x25/0x31/0x33/0x35/0x3B/0x41/0x43/
#      0x45/0x47/0x51/0x55/0x57/0x61/0x63/0x91/0x93/0xA1/0xA3）
#   4. 按协议应答充电桩（0x02/0x04/0x06/0x0A/0x32/0x40/0xA2 ...）
#   5. 提供下行接口（0x34 远程启机 / 0x36 远程停机 / 0x12 召唤实时数据
#      / 0x56 对时 / 0x52 工作参数 / 0x58 计费模型 / 0x92 重启 / 0x94 远程升级）
#   6. 全量报文 JSON 日志，最新实时数据内存缓存（可扩展入库）
#
# 用法：
#   python3 ykc_server.py --host 0.0.0.0 --port 8765
# =====================================================================

import argparse
import json
import logging
import secrets
import socket
import struct
import threading
import time
from datetime import datetime, timedelta
from socketserver import ThreadingTCPServer, BaseRequestHandler
import base64
import hashlib
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SOF = 0x68  # 起始标志

# =====================================================================
# CRC16 校验（附录 13.2，多项式表与协议文档一致）
# =====================================================================
def _build_crc_table():
    """按附录13.2算法生成标准 CRC-16/MODBUS 查表（反射多项式 0xA001）。
    注意：早期版本手抄的 gabyCRCHi/gabyCRCLo 码表与文档样例不符，
    会导致所有帧 CRC 校验失败，现已改为程序生成，杜绝转录错误。"""
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _build_crc_table()


def crc16(data: bytes) -> int:
    # 标准 CRC-16/MODBUS：初值 0xFFFF；返回值高字节在前（与文档 C 函数一致），发送时低字节在前
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


# =====================================================================
# 帧编解码（3.1 应用层数据结构）
# 帧格式: 0x68 | 数据长度 | 序列号(2) | 加密标志 | 帧类型 | 消息体 | CRC(2)
# =====================================================================
class FrameError(Exception):
    pass


def build_frame(seq: int, ftype: int, body: bytes = b"", encrypt: int = 0) -> bytes:
    payload = struct.pack("<H", seq & 0xFFFF) + bytes([encrypt & 0xFF, ftype & 0xFF]) + body
    if len(payload) > 0xFF:
        raise FrameError("消息体过长(>200字节约束)")
    frame = bytes([SOF, len(payload)]) + payload
    return frame + struct.pack("<H", crc16(payload))  # CRC 低字节在前


def try_parse_frame(buf: bytes):
    # 从接收缓冲区提取一帧，返回 (seq, encrypt, ftype, body, consumed)；数据不足返回 None
    if len(buf) < 4:
        return None
    if buf[0] != SOF:
        raise FrameError("起始符错误: 0x%02X" % buf[0])
    length = buf[1]
    total = 2 + length + 2
    if len(buf) < total:
        return None
    payload = buf[2:2 + length]
    crc_recv = struct.unpack("<H", buf[2 + length:total])[0]
    if crc16(payload) != crc_recv:
        crc_be = struct.unpack(">H", buf[2 + length:total])[0]
        if crc16(payload) != crc_be:   # 兼容部分固件"高字节在前"的变体
            raise FrameError("CRC 校验失败: 计算0x%04X 收到(低前)0x%04X 帧=%s"
                             % (crc16(payload), crc_recv,
                                buf[:total].hex().upper()))
    seq = struct.unpack("<H", payload[0:2])[0]
    enc, ftype = payload[2], payload[3]
    return seq, enc, ftype, payload[4:], total


# =====================================================================
# 基础数据类型编解码（3.2 数据格式定义）
# =====================================================================
def bcd_to_str(data: bytes) -> str:
    # BCD 码 -> 字符串，前导零保留（桩编号等定长字段）
    return "".join("%02X" % b for b in data)


def str_to_bcd(s: str, length: int) -> bytes:
    s = s.strip()[:length * 2].rjust(length * 2, "0")
    return bytes(int(s[i:i + 2], 16) for i in range(0, len(s), 2))


def cp56time2a_decode(data7: bytes) -> str:
    # CP56Time2a（3.2 节）
    ms = data7[0] | (data7[1] << 8)
    minute = data7[2] & 0x3F
    hour = data7[3] & 0x1F
    day = data7[4] & 0x1F
    month = data7[5] & 0x0F
    year = 2000 + (data7[6] & 0x7F)
    return "%04d-%02d-%02d %02d:%02d:%02d.%03d" % (
        year, month, day, hour, minute, ms // 1000, ms % 1000)


def cp56time2a_encode(dt: datetime = None) -> bytes:
    dt = dt or datetime.now()
    ms = dt.second * 1000 + dt.microsecond // 1000
    return bytes([
        ms & 0xFF, (ms >> 8) & 0xFF,
        dt.minute & 0x3F,
        dt.hour & 0x1F,
        dt.day & 0x1F,
        dt.month & 0x0F,
        (dt.year - 2000) & 0x7F])


def bin_le(data: bytes) -> int:        # BIN 码，低位在前
    return int.from_bytes(data, "little")


def ascii_str(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("ascii", errors="replace")


# =====================================================================
# 报文字段定义表：帧类型 -> (帧名称, [(字段名, 格式, 说明), ...])
# 格式: bcd7/bcd8/bcd10/bcd16/bcd6  BCD 码定长
#       bin1/bin2/bin4/bin5/bin8    BIN 码(小端)
#       bin3hex/bin4hex             原样 HEX 字符串
#       ascii8/ascii16/ascii17/ascii32
#       cp56                        CP56Time2a 时间
# =====================================================================
F = lambda n, f, d="": (n, f, d)

SPECS = {
    0x01: ("充电桩登录认证", [
        F("pile_no", "bcd7", "桩编码"), F("pile_type", "bin1", "0直流 1交流"),
        F("gun_count", "bin1", "充电枪数量"), F("proto_ver", "bin1", "协议版本*10"),
        F("prog_ver", "ascii8", "程序版本"), F("net_type", "bin1", "0SIM 1LAN 2WAN"),
        F("sim_no", "bcd10", "SIM卡号"), F("operator", "bin1", "运营商")]),
    0x02: ("登录认证应答", [F("pile_no", "bcd7"), F("result", "bin1", "0成功 1失败")]),
    0x03: ("充电桩心跳包", [
        F("pile_no", "bcd7"), F("gun_no", "bcd1"), F("gun_status", "bin1", "0正常 1故障")]),
    0x04: ("心跳包应答", [F("pile_no", "bcd7"), F("gun_no", "bcd1"), F("ack", "bin1")]),
    0x05: ("计费模型验证请求", [F("pile_no", "bcd7"), F("model_no", "bcd2", "计费模型编号")]),
    0x06: ("计费模型验证请求应答", [
        F("pile_no", "bcd7"), F("model_no", "bcd2"), F("result", "bin1", "0一致 1不一致")]),
    0x09: ("充电桩计费模型请求", [F("pile_no", "bcd7")]),
    0x0A: ("计费模型请求应答", [F("pile_no", "bcd7"), F("model_no", "bcd2"),
        F("jian_price", "bin4", "尖电费费率(5位小数)"), F("jian_svc", "bin4", "尖服务费费率"),
        F("feng_price", "bin4"), F("feng_svc", "bin4"),
        F("ping_price", "bin4"), F("ping_svc", "bin4"),
        F("gu_price", "bin4"), F("gu_svc", "bin4"),
        F("loss_ratio", "bin1", "计损比例"), F("periods", "bin48", "48个时段费率号")]),
    0x12: ("读取实时监测数据", [F("pile_no", "bcd7"), F("gun_no", "bcd1")]),
    0x13: ("上传实时监测数据", [
        F("serial_no", "bcd16", "交易流水号"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("status", "bin1", "0离线 1故障 2空闲 3充电"),
        F("gun_returned", "bin1", "0否 1是 2未知"), F("gun_plugged", "bin1", "0否 1是"),
        F("out_voltage", "bin2", "输出电压(0.1V)"), F("out_current", "bin2", "输出电流(0.1A)"),
        F("gun_temp", "bin1", "枪线温度(偏移-50)"), F("gun_line_code", "bin8hex", "枪线编码"),
        F("soc", "bin1"), F("batt_max_temp", "bin1", "偏移-50"),
        F("charge_time", "bin2", "累计充电时间min"), F("remain_time", "bin2", "剩余时间min"),
        F("energy", "bin4", "充电度数(4位小数)"), F("loss_energy", "bin4", "计损充电度数"),
        F("amount", "bin4", "已充金额(4位小数)"), F("hw_fault", "bin2", "硬件故障位图")]),
    0x15: ("充电握手", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("bms_proto_ver", "bin3hex", "BMS通信协议版本"),
        F("batt_type", "bin1", "电池类型"), F("batt_capacity", "bin2", "0.1Ah/位"),
        F("batt_total_vol", "bin2", "0.1V/位"),
        F("batt_vendor", "ascii4", "电池厂商"), F("batt_sn", "bin4hex", "电池组序号"),
        F("prod_year", "bin1", "+1985"), F("prod_month", "bin1"), F("prod_day", "bin1"),
        F("charge_count", "bin3", "充电次数"), F("ownership", "bin1", "0租赁 1自有"),
        F("reserved", "bin1"), F("vin", "ascii17", "VIN"),
        F("sw_ver", "bin8hex", "BMS软件版本号")]),
    0x17: ("参数配置", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("max_cell_vol", "bin2", "单体最高允许充电电压(0.01V)"),
        F("max_charge_cur", "bin2", "最高允许充电电流(0.1A,-400偏移)"),
        F("batt_energy", "bin2", "标称总能量(0.1kWh)"),
        F("max_total_vol", "bin2", "最高允许总电压(0.1V)"),
        F("max_temp", "bin1", "最高允许温度(偏移-50)"),
        F("soc", "bin2", "0.1%/位"), F("batt_vol", "bin2", "当前电池总电压"),
        F("pile_max_out_vol", "bin2", "0.1V/位"), F("pile_min_out_vol", "bin2", "0.1V/位"),
        F("pile_max_out_cur", "bin2", "0.1A/位,-400偏移"),
        F("pile_min_out_cur", "bin2", "0.1A/位,-400偏移")]),
    0x19: ("充电结束", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("end_soc", "bin1"), F("min_cell_vol", "bin2", "0.01V"),
        F("max_cell_vol", "bin2", "0.01V"), F("min_temp", "bin1", "偏移-50"),
        F("max_temp", "bin1", "偏移-50"), F("total_time", "bin2", "min"),
        F("output_energy", "bin2", "0.1kWh"), F("charger_no", "bin4", "充电机编号")]),
    0x1B: ("错误报文", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("error_bits", "bin8hex", "错误位域(序号4-25)")]),
    0x1D: ("充电阶段BMS中止", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("bms_stop_reason", "bin1"), F("bms_stop_fault", "bin2"),
        F("bms_stop_error", "bin1")]),
    0x21: ("充电阶段充电机中止", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("charger_stop_reason", "bin1"), F("charger_stop_fault", "bin2"),
        F("charger_stop_error", "bin1")]),
    0x23: ("BMS需求与充电机输出", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("bms_vol_need", "bin2", "0.1V"), F("bms_cur_need", "bin2", "0.1A,-400偏移"),
        F("charge_mode", "bin1", "1恒压 2恒流"),
        F("bms_vol_meas", "bin2"), F("bms_cur_meas", "bin2"),
        F("max_cell_vol", "bin2", "高12位电压/低4位组号"), F("soc", "bin1"),
        F("remain_time", "bin2", "min"),
        F("out_vol", "bin2", "电桩电压输出"), F("out_cur", "bin2", "电桩电流输出"),
        F("total_time", "bin2", "min")]),
    0x25: ("充电过程BMS信息", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("max_cell_no", "bin1"), F("max_temp", "bin1", "偏移-50"),
        F("max_temp_point", "bin1"), F("min_temp", "bin1", "偏移-50"),
        F("min_temp_point", "bin1"),
        F("bit_9_16", "bin1hex", "单体电压/SOC/过流/温度/绝缘/连接状态"),
        F("bit_17_24", "bin1hex", "充电禁止/预留")]),
    0x31: ("充电桩主动申请启动充电", [
        F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("start_mode", "bin1", "1刷卡 2帐号 3VIN"),
        F("need_pwd", "bin1", "0不需要 1需要"),
        F("account_or_card", "bin8hex", "账号或物理卡号"),
        F("password_md5", "bin16hex", "16位MD5(小写)"),
        F("vin", "ascii17")]),
    0x32: ("运营平台确认启动充电", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("logic_card_no", "bcd8", "逻辑卡号"), F("balance", "bin4", "余额(2位小数)"),
        F("auth_result", "bin1", "0失败 1成功"), F("fail_reason", "bin1")]),
    0x33: ("远程启动充电命令回复", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("start_result", "bin1", "0失败 1成功"), F("fail_reason", "bin1")]),
    0x34: ("运营平台远程控制启机", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("logic_card_no", "bcd8"), F("phys_card_no", "bin8hex"),
        F("balance", "bin4", "余额(2位小数)")]),
    0x35: ("远程停机命令回复", [
        F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("stop_result", "bin1", "0失败 1成功"), F("fail_reason", "bin1")]),
    0x36: ("运营平台远程停机", [F("pile_no", "bcd7"), F("gun_no", "bcd1")]),
    0x3B: ("交易记录", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("start_time", "cp56"), F("end_time", "cp56"),
        F("jian_price", "bin4", "5位小数"), F("jian_energy", "bin4", "4位小数"),
        F("jian_loss_energy", "bin4"), F("jian_amount", "bin4", "4位小数"),
        F("feng_price", "bin4"), F("feng_energy", "bin4"),
        F("feng_loss_energy", "bin4"), F("feng_amount", "bin4"),
        F("ping_price", "bin4"), F("ping_energy", "bin4"),
        F("ping_loss_energy", "bin4"), F("ping_amount", "bin4"),
        F("gu_price", "bin4"), F("gu_energy", "bin4"),
        F("gu_loss_energy", "bin4"), F("gu_amount", "bin4"),
        F("meter_start", "bin5", "电表总起值(4位小数)"),
        F("meter_end", "bin5", "电表总止值(4位小数)"),
        F("total_energy", "bin4"), F("total_loss_energy", "bin4"),
        F("total_amount", "bin4", "消费金额"),
        F("vin", "ascii17", "正序"), F("trade_flag", "bin1", "1app 2卡 4离线卡 5VIN"),
        F("trade_time", "cp56"), F("stop_reason", "bin1", "见附录13.1"),
        F("phys_card_no", "bin8hex")]),
    0x40: ("交易记录确认", [F("serial_no", "bcd16"), F("result", "bin1", "0成功 1非法账单")]),
    0x41: ("余额更新应答", [
        F("pile_no", "bcd7"), F("phys_card_no", "bin8hex"), F("result", "bin1")]),
    0x42: ("远程账户余额更新", [
        F("pile_no", "bcd7"), F("gun_no", "bcd1"), F("phys_card_no", "bin8hex"),
        F("new_balance", "bin4", "2位小数")]),
    0x43: ("离线卡数据同步应答", [
        F("pile_no", "bcd7"), F("save_result", "bin1", "0失败 1成功"),
        F("fail_reason", "bin1", "1卡号格式错 2空间不足")]),
    0x44: ("离线卡数据同步", [F("pile_no", "bcd7"), F("card_count", "bin1", "最大15")]),
    0x45: ("离线卡数据清除应答", [F("pile_no", "bcd7")]),  # 变长: (物理卡号8+结果1+原因1)*N
    0x46: ("离线卡数据清除", [F("pile_no", "bcd7"), F("card_count", "bin1", "最大24")]),
    0x47: ("离线卡数据查询应答", [F("pile_no", "bcd7")]),  # 变长: (物理卡号8+结果1)*N
    0x48: ("离线卡数据查询", [F("pile_no", "bcd7"), F("card_count", "bin1", "最大26")]),
    0x51: ("充电桩工作参数设置应答", [
        F("pile_no", "bcd7"), F("result", "bin1", "0失败 1成功")]),
    0x52: ("充电桩工作参数设置", [
        F("pile_no", "bcd7"), F("work_enable", "bin1", "0允许 1停用"),
        F("max_power", "bin1", "百分比30-100")]),
    0x55: ("对时设置应答", [F("pile_no", "bcd7"), F("current_time", "cp56")]),
    0x56: ("对时设置", [F("pile_no", "bcd7"), F("current_time", "cp56")]),
    0x57: ("计费模型应答", [F("pile_no", "bcd7"), F("result", "bin1")]),
    0x58: ("计费模型设置", [F("pile_no", "bcd7"), F("model_no", "bcd2")]),  # 同0x0A结构
    0x61: ("地锁数据上送", [
        F("pile_no", "bcd7"), F("gun_no", "bin1"),
        F("lock_status", "bin1", "0未到位 0x55升到位 0xFF降到位"),
        F("car_status", "bin1", "0无车 0xFF有车"),
        F("lock_battery", "bin1", "电量%"), F("alarm", "bin1", "0正常"),
        F("reserved", "bin4hex")]),
    0x62: ("遥控地锁升降命令", [
        F("pile_no", "bcd7"), F("gun_no", "bin1"),
        F("up_down", "bin1", "0x55升锁 0xFF降锁"), F("reserved", "bin4hex")]),
    0x63: ("充电桩返回数据(上行)", [
        F("pile_no", "bcd7"), F("gun_no", "bin1"),
        F("lock_ack", "bin1", "1成功 0失败"), F("reserved", "bin4hex")]),
    0x91: ("远程重启应答", [F("pile_no", "bcd7"), F("result", "bin1")]),
    0x92: ("远程重启", [F("pile_no", "bcd7"), F("ctrl", "bin1", "1立即 2空闲")]),
    0x93: ("远程更新应答", [
        F("pile_no", "bcd7"), F("status", "bin1", "0成功 1编号错 2型号不符 3下载超时")]),
    0x94: ("远程更新", [
        F("pile_no", "bcd7"), F("pile_model", "bin1", "1直流 2交流"),
        F("pile_power", "bin2"), F("ftp_host", "ascii16"), F("ftp_port", "bin2"),
        F("ftp_user", "ascii16"), F("ftp_pwd", "ascii16"), F("file_path", "ascii32"),
        F("ctrl", "bin1", "1立即 2空闲"), F("timeout_min", "bin1")]),
    0xA1: ("充电桩主动申请并充充电", [
        F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("start_mode", "bin1", "1刷卡 2帐号 3VIN"),
        F("need_pwd", "bin1"), F("account_or_card", "bin8hex"),
        F("password_md5", "bin16hex"), F("vin", "ascii17"),
        F("master_flag", "bin1", "0主枪 1辅枪"), F("parallel_no", "bcd6", "并充序号")]),
    0xA2: ("运营平台确认并充启动充电", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("logic_card_no", "bcd8"), F("balance", "bin4"),
        F("auth_result", "bin1"), F("fail_reason", "bin1"),
        F("parallel_no", "bcd6")]),
    0xA3: ("远程并充启机命令回复", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("start_result", "bin1"), F("fail_reason", "bin1"),
        F("master_flag", "bin1"), F("parallel_no", "bcd6")]),
    0xA4: ("运营平台远程控制并充启机", [
        F("serial_no", "bcd16"), F("pile_no", "bcd7"), F("gun_no", "bcd1"),
        F("logic_card_no", "bcd8"), F("phys_card_no", "bin8hex"), F("balance", "bin4"),
        F("parallel_no", "bcd6")]),
}

FRAME_NAMES = {k: v[0] for k, v in SPECS.items()}

_FMT_LEN = {
    "bcd1": 1, "bcd2": 2, "bcd6": 6, "bcd7": 7, "bcd8": 8, "bcd10": 10, "bcd16": 16,
    "bin1": 1, "bin2": 2, "bin4": 4, "bin5": 5, "bin8": 8, "bin48": 48,
    "bin1hex": 1, "bin2hex": 2, "bin3hex": 3, "bin4hex": 4, "bin8hex": 8,
    "bin16hex": 16,
    "ascii4": 4, "ascii8": 8, "ascii16": 16, "ascii17": 17, "ascii32": 32,
    "cp56": 7,
}


def decode_field(fmt: str, data: bytes):
    if fmt.startswith("bcd"):
        return bcd_to_str(data)
    if fmt == "bin48":
        return list(data)
    if fmt.startswith("bin") and fmt.endswith("hex"):
        return data.hex().upper()
    if fmt.startswith("bin"):
        return bin_le(data)
    if fmt.startswith("ascii"):
        return ascii_str(data)
    if fmt == "cp56":
        return cp56time2a_decode(data)
    return data.hex()


def parse_body(ftype: int, body: bytes) -> dict:
    # 按字段定义表解析消息体；变长帧(0x44/0x45/0x46/0x47/0x48)特殊处理
    out = {}
    if ftype in (0x44, 0x46, 0x48):            # 离线卡同步/清除/查询(下行)，变长卡组
        out["pile_no"] = bcd_to_str(body[0:7])
        out["card_count"] = body[7]
        pos, cards = 8, []
        group_len = 16 if ftype == 0x44 else 8  # 0x44: 逻辑卡号8+物理卡号8
        while pos + group_len <= len(body):
            grp = {"logic_card_no": bcd_to_str(body[pos:pos+8])} if ftype == 0x44 else {}
            if ftype == 0x44:
                grp["phys_card_no"] = body[pos+8:pos+16].hex().upper()
            else:
                grp["phys_card_no"] = body[pos:pos+8].hex().upper()
            cards.append(grp)
            pos += group_len
        out["cards"] = cards
        return out
    if ftype in (0x45, 0x47):                  # 清除/查询应答(上行)，变长
        out["pile_no"] = bcd_to_str(body[0:7])
        pos, cards = 7, []
        step = 10 if ftype == 0x45 else 9
        while pos + step <= len(body):
            grp = {"phys_card_no": body[pos:pos+8].hex().upper()}
            if ftype == 0x45:
                grp["result"] = body[pos+8]; grp["fail_reason"] = body[pos+9]
            else:
                grp["exist"] = body[pos+8]
            cards.append(grp)
            pos += step
        out["cards"] = cards
        return out

    spec = SPECS.get(ftype)
    if not spec:
        return {"raw": body.hex().upper()}
    pos = 0
    for name, fmt, _desc in spec[1]:
        n = _FMT_LEN.get(fmt)
        if n is None or pos + n > len(body):
            out.setdefault("_truncated", True)
            break
        out[name] = decode_field(fmt, body[pos:pos + n])
        pos += n
    if pos < len(body):                         # 多余字节（协议文档与实现可能有微小出入）
        out["_extra"] = body[pos:].hex().upper()
    return out


# =====================================================================
# 平台配置与数据缓存
# =====================================================================
class Config:
    def __init__(self):
        # 计费模型：费率精确到五位小数（0x0A/0x58 帧），时段费率号 0尖1峰2平3谷
        self.billing_model_no = "0100"
        self.rates = {"jian": (80000, 40000), "feng": (80000, 40000),
                      "ping": (80000, 40000), "gu": (80000, 40000)}  # (电费, 服务费)
        self.loss_ratio = 0
        self.periods = [2] * 48                   # 默认全部平费率
        self.billing_match = True                 # 0x06 验证结果：True=一致
        self.auth_pass = True                     # 0x32 鉴权结果：True=成功
        self.logic_card_no = "1000000000000000"


CFG = Config()

log = logging.getLogger("ykc")


# ---------- 交易记录持久化（transactions.json，重启不丢） ----------
TX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transactions.json")
TX_LOCK = threading.RLock()


def _load_transactions():
    try:
        with open(TX_FILE, encoding="utf-8") as f:
            return json.load(f).get("transactions", [])
    except Exception:
        return []


def _save_transactions(data_list):
    with TX_LOCK:
        with open(TX_FILE, "w", encoding="utf-8") as f:
            json.dump({"transactions": data_list[-5000:]}, f, ensure_ascii=False, indent=1)


class DataStore:
    # 内存缓存：最新实时数据/交易记录，可在此扩展数据库持久化
    def __init__(self):
        self._lock = threading.Lock()
        self.latest = {}      # (pile_no, gun) -> 0x13 解析结果
        # 0x3B 交易记录列表（启动时从 transactions.json 恢复）
        self.transactions = [(0, d) for d in _load_transactions()]

    def save_realtime(self, pile_no, gun_no, data):
        with self._lock:
            self.latest[(pile_no, gun_no)] = (time.time(), data)

    def save_transaction(self, data):
        with self._lock:
            self.transactions.append((time.time(), data))
            try:
                _save_transactions([d for (_t, d) in self.transactions])
            except Exception:
                pass

    def latest_of(self, pile_no):
        with self._lock:
            return {gun: data for (pn, gun), (_t, data) in self.latest.items()
                    if pn == pile_no}

    def all_transactions(self):
        with self._lock:
            return [d for (_t, d) in self.transactions]

    def get_session(self, pile_no):
        with SessionRegistry.lock:
            return SessionRegistry.sessions.get(pile_no)


class SessionRegistry:
    sessions = {}     # pile_no -> PileSession
    lock = threading.Lock()

    @classmethod
    def register(cls, pile_no, session):
        with cls.lock:
            cls.sessions[pile_no] = session

    @classmethod
    def unregister(cls, pile_no, session):
        with cls.lock:
            if cls.sessions.get(pile_no) is session:
                del cls.sessions[pile_no]


STORE = DataStore()


def make_serial_no(pile_no_bcd7: bytes, gun_no: int, seq: int = 0) -> bytes:
    # 交易流水号 = 桩号(7) + 枪号(1) + 年月日时分秒(6) + 自增序号(2)
    now = datetime.now()
    ts = str_to_bcd(now.strftime("%y%m%d%H%M%S"), 6)
    return pile_no_bcd7 + str_to_bcd(str(gun_no), 1) + ts + struct.pack("<H", seq)


def build_billing_model_body(pile_no_bcd7: bytes) -> bytes:
    # 0x0A / 0x58 计费模型消息体
    r = CFG.rates
    body = pile_no_bcd7 + str_to_bcd(CFG.billing_model_no, 2)
    for k in ("jian", "feng", "ping", "gu"):
        body += struct.pack("<I", r[k][0]) + struct.pack("<I", r[k][1])
    body += bytes([CFG.loss_ratio & 0xFF]) + bytes(CFG.periods)
    return body


# =====================================================================
# 充电桩会话：报文应答 + 下行命令接口
# =====================================================================
class PileSession:
    def __init__(self, request, client_addr):
        self.request = request
        self.client_addr = client_addr
        self.send_lock = threading.Lock()
        self.tx_seq = 0
        self.pile_no = None          # BCD 字符串
        self.pile_no_bcd = None      # 7字节
        self.serial_seq = 0
        self.connected_at = datetime.now().isoformat()
        self.pending_start = {}   # 0x34 流水号hex -> [event, result]，等待 0x33 回复
        self.gun_locks = {n: threading.RLock() for n in range(1, 100)}
        self.customer_stops = {}
        self.last_seen = time.time()   # 最近收到任一帧的时间(看门狗判定离线用)
        self.watchdog_fired = False    # 看门狗已广播离线(避免重复)
        self.pending_ack = {}          # 应答帧hex -> [event, box] (0x55/0x57/0x51/0x91/0x13)

    # ---------- 底层收发 ----------
    def send_frame(self, ftype: int, body: bytes = b""):
        with self.send_lock:
            frame = build_frame(self.tx_seq, ftype, body)
            self.request.sendall(frame)
            self.tx_seq = (self.tx_seq + 1) & 0xFFFF
        log.info(">> %s (%s) %s", FRAME_NAMES.get(ftype, hex(ftype)), hex(ftype),
                 frame.hex().upper())
        try:
            WSHub.broadcast({"type": "frame", "dir": "down", "pile_no": self.pile_no,
                             "ftype": ftype, "name": FRAME_NAMES.get(ftype, hex(ftype)),
                             "time": datetime.now().strftime("%H:%M:%S"),
                             "data": parse_body(ftype, body)})
        except Exception:
            pass

    def reply(self, req_seq: int, ftype: int, body: bytes = b""):
        # 应答帧序列号与请求帧保持一致
        frame = build_frame(req_seq, ftype, body)
        with self.send_lock:
            self.request.sendall(frame)
        log.info(">> %s (%s) seq=%d %s", FRAME_NAMES.get(ftype, hex(ftype)),
                 hex(ftype), req_seq, frame.hex().upper())
        try:
            WSHub.broadcast({"type": "frame", "dir": "down", "pile_no": self.pile_no,
                             "ftype": ftype, "name": FRAME_NAMES.get(ftype, hex(ftype)),
                             "time": datetime.now().strftime("%H:%M:%S"),
                             "data": parse_body(ftype, body)})
        except Exception:
            pass

    # ---------- 上行报文处理（收到充电桩报文后调用） ----------
    def dispatch(self, seq, enc, ftype, body, raw):
        self.last_seen = time.time()
        name = FRAME_NAMES.get(ftype, "未知帧")
        log.info("<< %s (%s) seq=%d 来自 %s", name, hex(ftype), seq, self.client_addr)
        handler = getattr(self, "h_%02X" % ftype, None)
        if handler:
            handler(seq, body)
        else:
            log.warning("  未注册处理函数，仅记录。body=%s", body.hex().upper())
        try:
            WSHub.broadcast({"type": "frame", "dir": "up", "pile_no": self.pile_no,
                             "ftype": ftype, "name": name, "seq": seq,
                             "time": datetime.now().strftime("%H:%M:%S"),
                             "data": parse_body(ftype, body)})
        except Exception:
            pass

    # ----- 0x01 登录认证 -> 0x02 -----
    def h_01(self, seq, body):
        d = parse_body(0x01, body)
        self.pile_no = d.get("pile_no")
        self.pile_no_bcd = str_to_bcd(self.pile_no, 7)
        SessionRegistry.register(self.pile_no, self)
        log.info("  桩[%s] 登录: %s", self.pile_no, json.dumps(d, ensure_ascii=False))
        self.reply(seq, 0x02, self.pile_no_bcd + b"\x00")   # 0x00 登录成功

    # ----- 0x03 心跳包 -> 0x04 -----
    def h_03(self, seq, body):
        d = parse_body(0x03, body)
        log.info("  桩[%s] 心跳: %s", self.pile_no, json.dumps(d, ensure_ascii=False))
        self._check_pile_mismatch(d)
        if d.get("gun_status") == 1:
            raise_fault(self.pile_no, d.get("gun_no", "01"), "枪故障", "心跳上报枪状态=故障")
        self.reply(seq, 0x04, body[:8] + b"\x00")           # 桩号7 + 枪号1 + 应答0

    # ----- 0x05 计费模型验证请求 -> 0x06 -----
    def h_05(self, seq, body):
        d = parse_body(0x05, body)
        log.info("  桩[%s] 计费模型验证: %s", self.pile_no, json.dumps(d))
        result = 0x00 if CFG.billing_match else 0x01
        self.reply(seq, 0x06, self.pile_no_bcd
                   + str_to_bcd(CFG.billing_model_no, 2) + bytes([result]))

    # ----- 0x09 计费模型请求 -> 0x0A -----
    def h_09(self, seq, body):
        log.info("  桩[%s] 请求计费模型", self.pile_no)
        self.reply(seq, 0x0A, build_billing_model_body(self.pile_no_bcd))

    # ----- 0x13 上传实时监测数据 -----
    def _check_pile_mismatch(self, d):
        # 桩在登录后变更了编号(未重新登录) → 自动迁移会话绑定并告警
        no = d.get("pile_no")
        if no and self.pile_no and no != self.pile_no:
            old_no = self.pile_no
            log.warning("[MIGRATE] 桩编号变更: 会话绑定 %s -> %s（建议重启桩以新编号重新登录）",
                        old_no, no)
            with SessionRegistry.lock:
                SessionRegistry.sessions.pop(old_no, None)
                SessionRegistry.sessions[no] = self
            self.pile_no = no
            self.pile_no_bcd = str_to_bcd(no, 7)
            try:
                WSHub.broadcast({"type": "offline", "pile_no": old_no,
                                 "time": _now_str(), "reason": "pile_id_changed"})
            except Exception:
                pass

    def _ack(self, key, d):
        # 唤醒等待中的下行命令(0x55/0x57/0x51/0x91/0x13)
        p = self.pending_ack.pop(key, None)
        if p:
            p[1].update(d)
            p[0].set()

    def h_13(self, seq, body):
        d = parse_body(0x13, body)
        self._check_pile_mismatch(d)
        STORE.save_realtime(self.pile_no, d.get("gun_no"), d)
        if CUSTOMER_APP:
            CUSTOMER_APP.realtime(d)
        self._ack("13", d)
        if d.get("status") == 1:
            raise_fault(self.pile_no, d.get("gun_no", "01"), "桩故障", "实时数据上报桩状态=故障")
        hw = d.get("hw_fault", 0)
        if isinstance(hw, int) and hw:
            raise_fault(self.pile_no, d.get("gun_no", "01"), "硬件故障",
                        "硬件故障位图=0x%04X" % hw)
        log.info("  实时数据: %s", json.dumps(d, ensure_ascii=False))

    # ----- 0x15/0x17/0x19/0x1B/0x1D/0x21/0x23/0x25 充电过程报文 -----
    def _log_only(self, ftype, body):
        d = parse_body(ftype, body)
        log.info("  %s: %s", FRAME_NAMES[ftype], json.dumps(d, ensure_ascii=False))
        return d

    h_15 = lambda s, q, b: s._log_only(0x15, b)
    h_17 = lambda s, q, b: s._log_only(0x17, b)
    h_19 = lambda s, q, b: s._log_only(0x19, b)
    h_1D = lambda s, q, b: s._log_only(0x1D, b)
    h_21 = lambda s, q, b: s._log_only(0x21, b)
    h_23 = lambda s, q, b: s._log_only(0x23, b)
    h_25 = lambda s, q, b: s._log_only(0x25, b)

    # ----- 0x31 主动申请启动充电 -> 0x32 -----
    def h_31(self, seq, body):
        d = parse_body(0x31, body)
        log.info("  桩[%s] 申请启动充电: %s", self.pile_no, json.dumps(d, ensure_ascii=False))
        serial = make_serial_no(self.pile_no_bcd, int(d.get("gun_no", 1)), self.serial_seq)
        self.serial_seq += 1
        auth = 0x01 if CFG.auth_pass else 0x00
        fail = 0x00 if CFG.auth_pass else 0x03
        body = serial + self.pile_no_bcd + str_to_bcd(str(d.get("gun_no", 1)), 1) \
            + str_to_bcd(CFG.logic_card_no, 8) + struct.pack("<I", 100000) \
            + bytes([auth, fail])
        self.reply(seq, 0x32, body)

    # ----- 0xA1 主动申请并充 -> 0xA2 -----
    def h_A1(self, seq, body):
        d = parse_body(0xA1, body)
        log.info("  桩[%s] 申请并充: %s", self.pile_no, json.dumps(d, ensure_ascii=False))
        serial = make_serial_no(self.pile_no_bcd, int(d.get("gun_no", 1)), self.serial_seq)
        self.serial_seq += 1
        auth = 0x01 if CFG.auth_pass else 0x00
        fail = 0x00 if CFG.auth_pass else 0x03
        body = serial + self.pile_no_bcd + str_to_bcd(str(d.get("gun_no", 1)), 1) \
            + str_to_bcd(CFG.logic_card_no, 8) + struct.pack("<I", 100000) \
            + bytes([auth, fail]) + str_to_bcd(d.get("parallel_no", "000000000000"), 6)
        self.reply(seq, 0xA2, body)

    # ----- 0x33 远程启机回复 / 0xA3 并充启机回复 -----
    def h_33(self, seq, body):
        d = self._log_only(0x33, body)
        pend = self.pending_start.get(str(d.get("serial_no", "")), None) if d.get("pile_no") == self.pile_no else None
        if pend:
            pend[1].update(d)
            pend[0].set()
        if CUSTOMER_APP and not d.get("_truncated"):
            CUSTOMER_APP.start_reply(d)
        if d.get("start_result") == 1:
            log.info("  订单 %s 远程启动成功", d.get("serial_no"))
        else:
            log.warning("  订单 %s 远程启动失败, 原因=%s",
                        d.get("serial_no"), d.get("fail_reason"))

    def h_A3(self, seq, body):
        self._log_only(0xA3, body)

    # ----- 0x35 远程停机回复 -----
    def h_35(self, seq, body):
        d = self._log_only(0x35, body)
        pending = self.customer_stops.get((seq, str(d.get("gun_no", "")))) if d.get("pile_no") == self.pile_no else None
        if pending:
            pending[1].update(d)
            pending[0].set()

    # ----- 0x3B 交易记录 -> 0x40 -----
    def h_3B(self, seq, body):
        d = parse_body(0x3B, body)
        if CUSTOMER_APP:
            CUSTOMER_APP.transaction(d)
        STORE.save_transaction(d)
        log.info("  交易记录: %s", json.dumps(d, ensure_ascii=False))
        serial_bcd = str_to_bcd(d.get("serial_no", "0"), 16)
        self.reply(seq, 0x40, serial_bcd + b"\x00")         # 0x00 上传成功

    # ----- 应答类上行帧（仅记录） -----
    h_41 = lambda s, q, b: s._log_only(0x41, b)
    h_45 = lambda s, q, b: s._log_only(0x45, b)
    h_47 = lambda s, q, b: s._log_only(0x47, b)
    h_63 = lambda s, q, b: s._log_only(0x63, b)
    h_93 = lambda s, q, b: s._log_only(0x93, b)

    def h_51(self, seq, body):
        d = self._log_only(0x51, body)
        self._ack("51", d)

    def h_55(self, seq, body):
        d = self._log_only(0x55, body)
        self._ack("55", d)

    def h_57(self, seq, body):
        d = self._log_only(0x57, body)
        self._ack("57", d)

    def h_91(self, seq, body):
        d = self._log_only(0x91, body)
        self._ack("91", d)

    def h_1B(self, seq, body):
        d = self._log_only(0x1B, body)
        raise_fault(self.pile_no, d.get("gun_no", "01"), "BMS错误",
                    "错误位域=%s" % d.get("error_bits", ""))

    # ----- 0x61 地锁数据上送 -----
    def h_61(self, seq, body):
        self._log_only(0x61, body)

    # ---------- 下行命令（供业务系统调用） ----------
    def dl_remote_start(self, gun_no, logic_card, phys_card_hex, balance_fen,
                        serial_no=None):
        # 0x34 运营平台远程控制启机
        serial = serial_no or make_serial_no(self.pile_no_bcd, gun_no, self.serial_seq)
        self.serial_seq += 1
        body = serial + self.pile_no_bcd + str_to_bcd(str(gun_no), 1) \
            + str_to_bcd(logic_card, 8) + bytes.fromhex(phys_card_hex.rjust(16, "0")) \
            + struct.pack("<I", balance_fen)
        key = serial.hex().upper()
        self.pending_start[key] = (threading.Event(), {})
        self.send_frame(0x34, body)
        return key

    def dl_remote_start_parallel(self, gun_no, logic_card, phys_card_hex,
                                 balance_fen, parallel_no):
        # 0xA4 远程并充启机
        serial = make_serial_no(self.pile_no_bcd, gun_no, self.serial_seq)
        self.serial_seq += 1
        body = serial + self.pile_no_bcd + str_to_bcd(str(gun_no), 1) \
            + str_to_bcd(logic_card, 8) + bytes.fromhex(phys_card_hex.rjust(16, "0")) \
            + struct.pack("<I", balance_fen) + str_to_bcd(parallel_no, 6)
        self.send_frame(0xA4, body)

    def dl_remote_stop(self, gun_no):
        # Both management and customer commands correlate by sequence and gun.
        event, box = threading.Event(), {}
        with self.send_lock:
            seq = self.tx_seq
            self.tx_seq = (seq + 1) & 0xFFFF
            key = (seq, "%02d" % gun_no)
            self.customer_stops[key] = (event, box)
            frame = build_frame(seq, 0x36, self.pile_no_bcd + str_to_bcd(str(gun_no), 1))
            try:
                self.request.sendall(frame)
            except OSError:
                self.customer_stops.pop(key, None)
                raise
        log.info(">> 远程停机(0x36) seq=%d gun=%d", seq, gun_no)
        return key

    def dl_read_realtime(self, gun_no):
        # 0x12 读取实时监测数据(等待 0x13 应答)
        self.pending_ack["13"] = (threading.Event(), {})
        self.send_frame(0x12, self.pile_no_bcd + str_to_bcd(str(gun_no), 1))

    def dl_sync_time(self):
        # 0x56 对时设置(等待 0x55 应答)
        self.pending_ack["55"] = (threading.Event(), {})
        self.send_frame(0x56, self.pile_no_bcd + cp56time2a_encode())

    def dl_set_work_params(self, work_enable=0, max_power=100):
        # 0x52 充电桩工作参数设置(等待 0x51 应答)
        self.pending_ack["51"] = (threading.Event(), {})
        self.send_frame(0x52, self.pile_no_bcd + bytes([work_enable, max_power]))

    def dl_set_billing_model(self):
        # 0x58 计费模型设置(等待 0x57 应答)
        self.pending_ack["57"] = (threading.Event(), {})
        self.send_frame(0x58, build_billing_model_body(self.pile_no_bcd))

    def dl_remote_reboot(self, ctrl=1):
        # 0x92 远程重启(等待 0x91 应答)
        self.pending_ack["91"] = (threading.Event(), {})
        self.send_frame(0x92, self.pile_no_bcd + bytes([ctrl]))

    def dl_remote_update(self, pile_model, pile_power, ftp_host, ftp_port,
                         ftp_user, ftp_pwd, file_path, ctrl=2, timeout_min=60):
        # 0x94 远程更新
        def pad(s, n):
            return s.encode("ascii")[:n].ljust(n, b"\x00")
        body = self.pile_no_bcd + bytes([pile_model]) + struct.pack("<H", pile_power) \
            + pad(ftp_host, 16) + struct.pack("<H", ftp_port) \
            + pad(ftp_user, 16) + pad(ftp_pwd, 16) + pad(file_path, 32) \
            + bytes([ctrl, timeout_min])
        self.send_frame(0x94, body)




# =====================================================================
# Web 服务层：REST API + WebSocket 实时推送 + 前端静态页面托管
#   前端访问: http://<服务器>:8080/      实时通道: ws://<服务器>:8080/ws
# =====================================================================
START_WAIT = 80    # 0x34 启机后等待 0x33 回复的最大秒数(协议要求桩90秒内回复)
STOP_WAIT = 20     # 0x36 停机后等待 0x35 回复的最大秒数
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 登录会话令牌（内存态，重启后失效）
SESSION_TOKENS = set()
SESSION_LOCK = threading.Lock()
DEFAULT_CREDENTIALS = {"admin": "123456"}   # 超级管理员，可用 --admin-user/--admin-pass 覆盖


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ws_send(sock, payload: bytes, opcode: int = 0x1):
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


def _ws_recv(sock):
    # 返回 (opcode, payload)；连接关闭/出错返回 None
    def read_exact(n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                return None
            buf += c
        return buf

    h = read_exact(2)
    if not h:
        return None
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", read_exact(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", read_exact(8))[0]
    mask = read_exact(4) if masked else b"\x00" * 4
    payload = read_exact(ln)
    if payload is None:
        return None
    return opcode, bytes(payload[i] ^ mask[i % 4] for i in range(ln))


class WSClient:
    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()


class WSHub:
    _clients = set()
    _lock = threading.Lock()

    @classmethod
    def add(cls, c):
        with cls._lock:
            cls._clients.add(c)

    @classmethod
    def remove(cls, c):
        with cls._lock:
            cls._clients.discard(c)

    @classmethod
    def broadcast(cls, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        with cls._lock:
            clients = list(cls._clients)
        dead = []
        for c in clients:
            try:
                with c.lock:
                    _ws_send(c.sock, data)
            except OSError:
                dead.append(c)
        for c in dead:
            cls.remove(c)


# ---------- 充电站管理（stations.json 持久化，与程序同目录） ----------
STATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations.json")
STATIONS_LOCK = threading.RLock()   # 同线程内 load->save 嵌套加锁，须用可重入锁


def load_stations():
    try:
        with open(STATIONS_FILE, encoding="utf-8") as f:
            return json.load(f).get("stations", [])
    except Exception:
        return []


def save_stations(stations):
    with STATIONS_LOCK:
        with open(STATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({"stations": stations}, f, ensure_ascii=False, indent=2)


def stations_snapshot():
    with STATIONS_LOCK:
        sts = [dict(x) for x in load_stations()]
    for st in sts:
        info = []
        for no in st.get("pile_nos", []):
            sess = STORE.get_session(no)
            info.append({"pile_no": no, "online": bool(sess),
                         "latest": STORE.latest_of(no) if sess else {}})
        st["piles"] = info
        st["online_count"] = sum(1 for x in info if x["online"])
    return sts


# ---------- 故障管理（faults.json 持久化） ----------
FAULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faults.json")
FAULTS_LOCK = threading.RLock()

FAULT_STATUS = {"active": "未处理", "dispatched": "已派发", "resolved": "已处理"}


def load_faults():
    try:
        with open(FAULTS_FILE, encoding="utf-8") as f:
            return json.load(f).get("faults", [])
    except Exception:
        return []


def save_faults(faults):
    with FAULTS_LOCK:
        with open(FAULTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"faults": faults}, f, ensure_ascii=False, indent=2)


def raise_fault(pile_no, gun_no, fault_type, detail):
    """记录一条现存故障；同桩同枪同类型且未解决的故障不重复记录"""
    gun_no = gun_no or "01"
    with FAULTS_LOCK:
        faults = load_faults()
        for x in faults:
            if (x.get("pile_no") == pile_no and x.get("gun_no") == gun_no
                    and x.get("fault_type") == fault_type
                    and x.get("status") in ("active", "dispatched")):
                return None
        fault = {"id": secrets.token_hex(4), "pile_no": pile_no, "gun_no": gun_no,
                 "fault_type": fault_type, "detail": detail, "status": "active",
                 "occur_time": _now_str(), "dispatch_time": None,
                 "resolve_time": None, "handler": None, "order": None}
        faults.append(fault)
        save_faults(faults)
    log.warning("[FAULT] 新故障: %s | 桩[%s]枪%s | %s", fault_type, pile_no, gun_no, detail)
    try:
        WSHub.broadcast({"type": "fault", "fault": fault, "time": _now_str()})
    except Exception:
        pass
    return fault


# ---------- 离线看门狗：心跳超时主动判离线并断开 ----------
class OfflineWatchdog(threading.Thread):
    def __init__(self, timeout=90):
        super().__init__(daemon=True)
        self.timeout = timeout          # 超过 N 秒无任何帧则判离线

    def run(self):
        while True:
            time.sleep(10)
            now = time.time()
            with SessionRegistry.lock:
                items = list(SessionRegistry.sessions.items())
            for no, sess in items:
                if now - sess.last_seen <= self.timeout:
                    continue
                log.warning("[WATCHDOG] 桩[%s] %ds 无数据，判定离线并断开", no, self.timeout)
                sess.watchdog_fired = True
                try:
                    WSHub.broadcast({"type": "offline", "pile_no": no,
                                     "time": _now_str(), "reason": "heartbeat_timeout"})
                except Exception:
                    pass
                try:
                    sess.request.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sess.request.close()
                except Exception:
                    pass
                with SessionRegistry.lock:
                    if SessionRegistry.sessions.get(no) is sess:
                        del SessionRegistry.sessions[no]


def _station_piles(name_or_id):
    """按站点名或ID返回其桩号集合；站点不存在返回空集"""
    if not name_or_id:
        return None
    for st in load_stations():
        if st.get("name") == name_or_id or st.get("id") == name_or_id:
            return set(st.get("pile_nos", []))
    return set()


def _tx_filter(station=None, day=None):
    """交易过滤：按站点、按日(today/yesterday)"""
    tx = STORE.all_transactions()
    piles = _station_piles(station)
    if piles is not None:
        tx = [t for t in tx if t.get("pile_no") in piles]
    if day in ("today", "yesterday"):
        d = (datetime.now() if day == "today"
             else datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        tx = [t for t in tx if str(t.get("trade_time", ""))[:10] == d
              or str(t.get("start_time", ""))[:10] == d]
    return tx


def _elec_svc_ratio():
    """电费/服务费拆分比例（取平台计费模型 平段 费率）"""
    pe, ps = CFG.rates["ping"]
    tot = pe + ps or 1
    return pe / tot, ps / tot


def clear_log_file():
    """清空日志文件内容（减小占用）。优先通过 logging FileHandler 流截断，
    防止文件描述符偏移残留导致稀疏文件/空行填充。"""
    path = os.path.abspath("ykc_server.log")
    done = False
    for h in list(log.handlers):
        if isinstance(h, logging.FileHandler) and \
                os.path.abspath(h.baseFilename) == path:
            h.acquire()
            try:
                h.stream.seek(0)
                h.stream.truncate()
                done = True
            finally:
                h.release()
    if not done:
        try:
            with open(path, "w"):
                pass
            done = True
        except Exception:
            pass
    return done


def read_log_lines(limit=200):
    # 与 logging FileHandler("ykc_server.log") 同一文件（进程工作目录下）
    try:
        path = os.path.abspath("ykc_server.log")
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-limit:]
    except Exception:
        return []


def piles_snapshot():
    out = []
    with SessionRegistry.lock:
        items = list(SessionRegistry.sessions.items())
    for no, s in items:
        out.append({"pile_no": no, "client": "%s:%s" % s.client_addr,
                    "connected_at": s.connected_at, "latest": STORE.latest_of(no)})
    return out


def make_api_handler(token, credentials=None):
    credentials = credentials or DEFAULT_CREDENTIALS
    require_auth = bool(token) or bool(credentials)   # 配置了凭据/静态令牌则始终要求登录
    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # ---------- 工具 ----------
        def _auth_ok(self):
            if not require_auth:
                return True                      # 完全未启用鉴权(无令牌且无凭据)
            t = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                t = auth[7:]
            if not t:
                t = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if token and t == token:
                return True
            with SESSION_LOCK:
                return t in SESSION_TOKENS

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self):
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            if not os.path.isfile(fp):
                return self._json({"error": "index.html 不存在，请将其放在程序同目录"}, 404)
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _log_download(self):
            path = os.path.abspath("ykc_server.log")
            if not os.path.isfile(path):
                return self._json({"error": "日志文件不存在"}, 404)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             'attachment; filename="ykc_server.log"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------- GET ----------
        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/index.html"):
                return self._static()          # 登录页免鉴权(否则令牌无从获取)
            if not self._auth_ok():
                return self._json({"error": "未授权"}, 401)
            if p == "/ws":
                return self._websocket()
            if p == "/api/piles":
                return self._json({"piles": piles_snapshot(), "time": _now_str(),
                                   "billing_model": CFG.billing_model_no})
            if p == "/api/transactions":
                return self._json({"transactions": STORE.all_transactions()})
            if p == "/api/stats":
                q = parse_qs(urlparse(self.path).query)
                tx = _tx_filter(q.get("station", [None])[0] or None,
                                q.get("day", [None])[0] or None)
                amount = sum(int(t.get("total_amount", 0)) for t in tx) / 10000
                re_, rs_ = _elec_svc_ratio()
                return self._json({
                    "tx_count": len(tx),
                    "tx_amount": round(amount, 2),
                    "tx_energy": round(sum(int(t.get("total_energy", 0)) for t in tx) / 10000, 4),
                    "elec_amount": round(amount * re_, 2),
                    "svc_amount": round(amount * rs_, 2),
                })
            if p == "/api/stats/trend":
                q = parse_qs(urlparse(self.path).query)
                try:
                    days = max(7, min(int(q.get("days", ["30"])[0]), 90))
                except ValueError:
                    days = 30
                buckets = {}
                for t in _tx_filter(q.get("station", [None])[0] or None):
                    d = str(t.get("trade_time") or t.get("start_time") or "")[:10]
                    if not d:
                        continue
                    b = buckets.setdefault(d, {"energy": 0.0, "count": 0, "amount": 0.0})
                    b["energy"] += int(t.get("total_energy", 0)) / 10000
                    b["amount"] += int(t.get("total_amount", 0)) / 10000
                    b["count"] += 1
                out = []
                for i in range(days - 1, -1, -1):
                    d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    b = buckets.get(d, {})
                    out.append({"date": d[5:], "energy": round(b.get("energy", 0), 4),
                                "count": b.get("count", 0), "amount": round(b.get("amount", 0), 2)})
                return self._json({"days": out})
            if p == "/api/stats/hourly":
                q = parse_qs(urlparse(self.path).query)
                buckets = {h: {"energy": 0.0, "count": 0, "amount": 0.0} for h in range(24)}
                for t in _tx_filter(q.get("station", [None])[0] or None):
                    ts = str(t.get("trade_time") or "")
                    if len(ts) < 13 or not ts[11:13].isdigit():
                        continue
                    b = buckets[int(ts[11:13])]
                    b["energy"] += int(t.get("total_energy", 0)) / 10000
                    b["amount"] += int(t.get("total_amount", 0)) / 10000
                    b["count"] += 1
                return self._json({"hours": [
                    {"hour": "%02d:00" % h, "energy": round(buckets[h]["energy"], 4),
                     "count": buckets[h]["count"], "amount": round(buckets[h]["amount"], 2)}
                    for h in range(24)]})
            if p == "/api/stations":
                return self._json({"stations": stations_snapshot()})
            if p == "/api/faults":
                with FAULTS_LOCK:
                    faults = list(reversed(load_faults()))
                return self._json({
                    "active": [f for f in faults if f.get("status") in ("active", "dispatched")],
                    "history": [f for f in faults if f.get("status") == "resolved"],
                })
            if p == "/api/logs/download":
                return self._log_download()
            if p == "/api/logs":
                q = parse_qs(urlparse(self.path).query)
                try:
                    limit = max(1, min(int(q.get("limit", ["200"])[0]), 1000))
                except ValueError:
                    limit = 200
                return self._json({"lines": read_log_lines(limit)})
            m = re.fullmatch(r"/api/piles/([0-9A-Fa-f]{14,16})", p)
            if m:
                no = m.group(1).upper()
                sess = STORE.get_session(no)
                if not sess:
                    return self._json({"error": "桩不在线"}, 404)
                return self._json({"pile_no": no, "client": str(sess.client_addr),
                                   "connected_at": sess.connected_at,
                                   "latest": STORE.latest_of(no)})
            return self._json({"error": "not found"}, 404)

        # ---------- POST ----------
        def _login(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                pl = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"error": "请求体不是合法JSON"}, 400)
            user, pwd = str(pl.get("username", "")), str(pl.get("password", ""))
            if credentials.get(user) != pwd:
                log.warning("[WEB] 登录失败: user=%s from %s", user, self.client_address)
                return self._json({"ok": False, "error": "用户名或密码错误"}, 401)
            t = secrets.token_hex(16)
            with SESSION_LOCK:
                SESSION_TOKENS.add(t)
            log.info("[WEB] 用户 %s 登录成功", user)
            return self._json({"ok": True, "token": t, "name": user})

        def _logout(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                with SESSION_LOCK:
                    SESSION_TOKENS.discard(auth[7:])
            return self._json({"ok": True})

        def _station_from_payload(self, pl, old=None):
            st = dict(old or {})
            if "name" in pl:
                st["name"] = str(pl["name"]).strip()
            if "address" in pl:
                st["address"] = str(pl.get("address", "")).strip()
            if "pile_nos" in pl:
                nos = pl["pile_nos"]
                if isinstance(nos, str):
                    nos = re.split(r"[,;\s]+", nos)
                st["pile_nos"] = sorted({re.sub(r"[^0-9A-Fa-f]", "", str(x)).upper()
                                         for x in nos} - {""})
            if not st.get("name"):
                raise ValueError("站点名称不能为空")
            return st

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/login":
                return self._login()
            if p == "/api/logout":
                return self._logout()
            if p == "/api/stations":                       # 新建站点
                if not self._auth_ok():
                    return self._json({"error": "未授权"}, 401)
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    pl = json.loads(self.rfile.read(length) or b"{}")
                    st = self._station_from_payload(pl)
                    st["id"] = secrets.token_hex(4)
                    with STATIONS_LOCK:
                        stations = load_stations()
                        stations.append(st)
                        save_stations(stations)
                    log.info("[WEB] 新建充电站: %s(%s) 桩:%s", st["name"], st["id"], st["pile_nos"])
                    return self._json({"ok": True, "station": st})
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
            if not self._auth_ok():
                return self._json({"error": "未授权"}, 401)
            p = urlparse(self.path).path
            if p == "/api/logs/clear":
                ok = clear_log_file()
                if ok:
                    log.info("[WEB] 日志文件已被手动清空 (来自 %s)", self.client_address)
                return self._json({"ok": ok})
            mf = re.fullmatch(r"/api/faults/([0-9a-fA-F]+)/(dispatch|resolve)", p)
            if mf:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    pl = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    return self._json({"error": "请求体不是合法JSON"}, 400)
                fault_id, action = mf.group(1), mf.group(2)
                with FAULTS_LOCK:
                    faults = load_faults()
                    fault = next((x for x in faults if x.get("id") == fault_id), None)
                    if not fault:
                        return self._json({"error": "故障记录不存在"}, 404)
                    if action == "dispatch":
                        person = str(pl.get("person", "")).strip()
                        if not person:
                            return self._json({"error": "请指定处理人员"}, 400)
                        fault["status"] = "dispatched"
                        fault["dispatch_time"] = _now_str()
                        fault["order"] = {"person": person,
                                          "note": str(pl.get("note", "")).strip(),
                                          "time": _now_str()}
                        log.info("[WEB] 故障 %s 已派发工单给 %s", fault_id, person)
                    else:
                        if fault.get("status") == "resolved":
                            return self._json({"error": "该故障已处理完成"}, 400)
                        fault["status"] = "resolved"
                        fault["resolve_time"] = _now_str()
                        fault["handler"] = str(pl.get("handler", "")).strip() or "未填写"
                        log.info("[WEB] 故障 %s 处理完成(%s)", fault_id, fault["handler"])
                    save_faults(faults)
                try:
                    WSHub.broadcast({"type": "fault_update", "time": _now_str()})
                except Exception:
                    pass
                return self._json({"ok": True, "fault": fault})
            m = re.fullmatch(r"/api/piles/([0-9A-Fa-f]{14,16})/(start|stop|cmd)", p)
            if not m:
                return self._json({"error": "not found"}, 404)
            no, action = m.group(1).upper(), m.group(2)
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"error": "请求体不是合法JSON"}, 400)
            sess = STORE.get_session(no)
            if not sess:
                return self._json({"error": "桩未连接，无法下发命令"}, 404)
            try:
                if action == "start":
                    return self._cmd_start(sess, payload)
                if action == "stop":
                    return self._cmd_stop(sess, payload)
                return self._cmd_other(sess, payload)
            except Exception as e:
                log.exception("命令下发失败")
                return self._json({"error": str(e)}, 500)

        def _cmd_start(self, sess, pl):
            gun = int(pl.get("gun_no", 1))
            if gun not in sess.gun_locks:
                return self._json({"error": "枪号无效"}, 400)
            with sess.gun_locks[gun]:
                if CUSTOMER_APP and CUSTOMER_APP.active(sess.pile_no, gun):
                    return self._json({"error": "该枪存在客户订单"}, 409)
                return self._cmd_start_locked(sess, pl)

        def _cmd_start_locked(self, sess, pl):
            gun = int(pl.get("gun_no", 1))
            logic = str(pl.get("logic_card_no") or CFG.logic_card_no)
            phys = re.sub(r"[^0-9A-Fa-f]", "", str(pl.get("phys_card_no", "0"))) or "0"
            balance = int(round(float(pl.get("balance", 1000)) * 100))   # 元 -> 分
            log.info("[WEB] 对桩[%s]枪%d下发远程启机(0x34)", sess.pile_no, gun)
            key = sess.dl_remote_start(gun, logic, phys, balance)
            ev, box = sess.pending_start.get(key, (None, None))
            if ev is not None and ev.wait(START_WAIT):
                ok = box.get("start_result") == 1
                sess.pending_start.pop(key, None)
                return self._json({"ok": ok, "reply": box})
            sess.pending_start.pop(key, None)
            return self._json({"ok": False,
                               "error": "等待充电桩回复(0x33)超时 %ds" % START_WAIT}, 504)

        def _cmd_stop(self, sess, pl):
            gun = int(pl.get("gun_no", 1))
            if gun not in sess.gun_locks:
                return self._json({"error": "枪号无效"}, 400)
            with sess.gun_locks[gun]:
                return self._cmd_stop_locked(sess, pl)

        def _cmd_stop_locked(self, sess, pl):
            gun = int(pl.get("gun_no", 1))
            log.info("[WEB] 对桩[%s]枪%d下发远程停机(0x36)", sess.pile_no, gun)
            key = sess.dl_remote_stop(gun)
            ev, box = sess.customer_stops[key]
            try:
                if ev.wait(STOP_WAIT):
                    return self._json({"ok": box.get("stop_result") == 1, "reply": box})
                return self._json({"ok": False, "error": "等待停机回复超时"}, 504)
            finally:
                sess.customer_stops.pop(key, None)

        # 需要等待桩应答的命令: 命令 -> (应答帧hex, 超时秒)
        ACK_WAIT = {"read_realtime": ("13", 10), "sync_time": ("55", 10),
                    "billing_model": ("57", 10), "work_params": ("51", 10),
                    "reboot": ("91", 10)}

        def _cmd_other(self, sess, pl):
            cmd = pl.get("cmd", "")
            gun = int(pl.get("gun_no", 1))
            if cmd == "read_realtime":
                sess.dl_read_realtime(gun)
            elif cmd == "sync_time":
                sess.dl_sync_time()
            elif cmd == "reboot":
                sess.dl_remote_reboot(int(pl.get("ctrl", 1)))
            elif cmd == "work_params":
                sess.dl_set_work_params(int(pl.get("work_enable", 0)),
                                        int(pl.get("max_power", 100)))
            elif cmd == "billing_model":
                sess.dl_set_billing_model()
            else:
                return self._json({"error": "未知命令: %s（可选 read_realtime/sync_time/"
                                            "reboot/work_params/billing_model）" % cmd}, 400)
            ack_key, wait = self.ACK_WAIT[cmd]
            ev, box = sess.pending_ack.get(ack_key, (None, None))
            if ev is not None and ev.wait(wait):
                log.info("[WEB] 桩[%s] 命令 %s 应答: %s", sess.pile_no, cmd, box)
                return self._json({"ok": True, "cmd": cmd, "reply": box})
            sess.pending_ack.pop(ack_key, None)
            return self._json({"ok": False, "cmd": cmd,
                               "error": "等待桩应答(0x%s)超时 %ds" % (ack_key, wait)}, 504)

        def _station_update(self, station_id, pl):
            if not self._auth_ok():
                return self._json({"error": "未授权"}, 401)
            with STATIONS_LOCK:
                stations = load_stations()
                for i, st in enumerate(stations):
                    if st.get("id") == station_id:
                        try:
                            stations[i] = self._station_from_payload(pl, st)
                        except ValueError as e:
                            return self._json({"error": str(e)}, 400)
                        save_stations(stations)
                        log.info("[WEB] 更新充电站 %s: %s", station_id, stations[i])
                        return self._json({"ok": True, "station": stations[i]})
            return self._json({"error": "站点不存在"}, 404)

        def _station_delete(self, station_id):
            if not self._auth_ok():
                return self._json({"error": "未授权"}, 401)
            with STATIONS_LOCK:
                stations = load_stations()
                new = [x for x in stations if x.get("id") != station_id]
                if len(new) == len(stations):
                    return self._json({"error": "站点不存在"}, 404)
                save_stations(new)
            log.info("[WEB] 删除充电站 %s", station_id)
            return self._json({"ok": True})

        def do_PUT(self):
            m = re.fullmatch(r"/api/stations/([0-9a-fA-F]+)", urlparse(self.path).path)
            if m:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    pl = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    return self._json({"error": "请求体不是合法JSON"}, 400)
                return self._station_update(m.group(1), pl)
            return self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            m = re.fullmatch(r"/api/stations/([0-9a-fA-F]+)", urlparse(self.path).path)
            if m:
                return self._station_delete(m.group(1))
            return self._json({"error": "not found"}, 404)

        # ---------- WebSocket ----------
        def _websocket(self):
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                return self._json({"error": "bad websocket handshake"}, 400)
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True

            client = WSClient(self.connection)
            WSHub.add(client)
            log.info("[WEB] WebSocket 客户端接入: %s", self.client_address)
            try:
                _ws_send(client.sock, json.dumps(
                    {"type": "snapshot", "time": _now_str(),
                     "piles": piles_snapshot()}, ensure_ascii=False).encode("utf-8"))
                while True:
                    fr = _ws_recv(client.sock)
                    if fr is None:
                        break
                    op, data = fr
                    if op == 0x8:      # close
                        break
                    if op == 0x9:      # ping -> pong
                        _ws_send(client.sock, data, opcode=0xA)
            except OSError:
                pass
            finally:
                WSHub.remove(client)
    return ApiHandler


class WebApiServer(threading.Thread):
    def __init__(self, host, port, token="", credentials=None):
        super().__init__(daemon=True)
        self.host, self.port, self.token = host, port, token
        self.credentials = credentials
        self.httpd = ThreadingHTTPServer((host, port), make_api_handler(token, credentials))

    def run(self):
        httpd = self.httpd
        log.info("Web/API/WS 服务已启动: http://%s:%d/  (WS: /ws)%s",
                 self.host, self.port, "  [Token鉴权已开启]" if self.token else "")
        httpd.serve_forever()


# =====================================================================
# TCP 服务
# =====================================================================
class YkcHandler(BaseRequestHandler):
    def handle(self):
        session = PileSession(self.request, self.client_address)
        log.info("桩连接建立: %s", self.client_address)
        buf = b""
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                # 流式粘包/拆包处理
                while buf:
                    if buf[0] != SOF:                   # 丢弃无效字节，重新同步
                        bad = buf.find(bytes([SOF]))
                        if bad < 0:
                            buf = b""
                            break
                        log.warning("丢弃 %d 个非法字节", bad)
                        buf = buf[bad:]
                        continue
                    try:
                        frame = try_parse_frame(buf)
                    except FrameError as e:
                        log.error("帧解析错误: %s, 丢弃首字节重新同步", e)
                        buf = buf[1:]
                        continue
                    if frame is None:                   # 半包，等待更多数据
                        break
                    seq, enc, ftype, body, consumed = frame
                    buf = buf[consumed:]
                    if enc != 0x00:
                        log.warning("收到加密帧(3DES)，本示例未实现解密: enc=0x%02X", enc)
                        continue
                    session.dispatch(seq, enc, ftype, body, buf)
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log.info("连接中断 %s: %s", self.client_address, e)
        finally:
            if session.pile_no:
                if not session.watchdog_fired:
                    try:
                        WSHub.broadcast({"type": "offline", "pile_no": session.pile_no,
                                         "time": datetime.now().strftime("%H:%M:%S")})
                    except Exception:
                        pass
                SessionRegistry.unregister(session.pile_no, session)
            log.info("桩连接关闭: %s (pile=%s)", self.client_address, session.pile_no)


CUSTOMER_APP = None

#!/usr/bin/env python3
"""Standalone customer accounts and page server; charging remains a frontend demo."""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ITERATIONS = 600000
QUESTIONS = ('movie', 'book', 'color', 'sport', 'disliked_food', 'first_phone_last4', 'respected_person', 'favorite_number')

def digest(value):
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return salt + ':' + hashed

def matches(value, stored):
    salt, expected = stored.split(':')
    actual = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return hmac.compare_digest(actual, expected)

def answer_text(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()

def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

class CustomerAccounts:
    def __init__(self, db, page, insecure=False):
        self.db, self.page, self.insecure = str(db), Path(page), insecure
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS resets(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
            ''')
        os.chmod(self.db, 0o600)
        self.dummy = digest('dummy-account-timing-value')
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        return c
    def limit(self, keys, maximum=5):
        now = time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM limits WHERE until < ?', (now,))
            rows = [c.execute('SELECT count FROM limits WHERE key=?', (k,)).fetchone() for k in keys]
            if any(r and r['count'] >= maximum for r in rows):
                return False
            for key in keys:
                c.execute('INSERT INTO limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now+600))
        return True
    def session(self, token):
        with self.connect() as c:
            return c.execute('SELECT u.id,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?', (token_hash(token),time.time())).fetchone()
    def issue(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            c.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash(token),user_id,time.time()+86400))
        return token

class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log request payloads, tokens, or URL query parameters.
            print('%s %s' % (self.client_address[0], fmt % args), flush=True)
        def log_request(self, code='-', size='-'):
            self.log_message('%s %s %s', self.command, urlsplit(self.path).path, code)
        def send(self, obj, status=200, cookie=None):
            body=json.dumps(obj,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            if cookie: self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(body)
        def cookie(self, token='', expire=False):
            return 'customer_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%s%s' % (token,0 if expire else 86400,'' if app.insecure else '; Secure')
        def token(self):
            try:
                cookies=SimpleCookie(self.headers.get('Cookie',''))
                return cookies['customer_session'].value if 'customer_session' in cookies else ''
            except Exception: return ''
        def do_GET(self):
            path=urlsplit(self.path).path
            if path in ('/','/charge.html','/index.html'):
                try: body=app.page.read_bytes()
                except OSError: return self.send({'error':'客户页面文件不存在'},503)
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Referrer-Policy','same-origin')
                self.end_headers(); self.wfile.write(body); return
            if path=='/api/customer/me':
                u=app.session(self.token())
                return self.send({'user':{'username':u['username']}},200) if u else self.send({'error':'请先登录'},401)
            if path=='/api/customer/config':
                return self.send({'insecure_test':app.insecure,'charging_demo':not hasattr(app,'device'),'test_balance_yuan':getattr(app,'balance_fen',0)/100})
            if hasattr(app, 'customer_get') and path.startswith('/api/customer/'):
                try:
                    u=app.session(self.token())
                    if not u: raise ApiError(401,'请先登录')
                    return self.send(app.customer_get(path, self.path, u['id']))
                except ApiError as e: return self.send({'error':e.message},e.status)
                except Exception: return self.send({'error':'服务暂时不可用'},500)
            self.send({'error':'接口不存在'},404)
        def do_POST(self):
            try:
                # Require a same-origin browser JSON request. No CORS support.
                origin=self.headers.get('Origin')
                host=self.headers.get('Host')
                if not origin or urlsplit(origin).netloc!=host or self.headers.get('X-Customer-Request')!='1':
                    raise ApiError(403,'请求来源无效，请从客户页面操作')
                if urlsplit(origin).scheme!='https' and not app.insecure:
                    raise ApiError(403,'账号功能需要HTTPS；HTTP仅允许显式测试模式')
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise ApiError(415,'需要JSON请求')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=8192: raise ApiError(400,'请求长度无效')
                self.connection.settimeout(10)
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict): raise ApiError(400,'请求格式错误')
                path=urlsplit(self.path).path
                if hasattr(app,'customer_post') and path.startswith('/api/customer/orders'):
                    u=app.session(self.token())
                    if not u: raise ApiError(401,'请先登录')
                    if not app.limit(['order:user:'+str(u['id'])],30): raise ApiError(429,'操作过于频繁，请稍后重试')
                    return self.send(app.customer_post(path,payload,u['id']))
                if path=='/api/customer/logout':
                    with app.connect() as c: c.execute('DELETE FROM sessions WHERE token=?',(token_hash(self.token()),))
                    return self.send({'ok':True},cookie=self.cookie(expire=True))
                if path not in ('/api/customer/register','/api/customer/login','/api/customer/recovery/verify','/api/customer/recovery/reset'):
                    raise ApiError(404,'接口不存在')
                ip=self.client_address[0]
                if not app.limit(['ip:'+ip],60): raise ApiError(429,'请求过于频繁，请10分钟后重试')
                if path.endswith('/reset'): return self.reset(payload)
                username=payload.get('username','')
                if not isinstance(username,str) or not re.fullmatch('[A-Za-z0-9_]{4,32}',username):
                    raise ApiError(400,'账号必须为4–32位字母、数字或下划线')
                username=username.lower()
                action=path.rsplit('/',1)[-1]
                if not app.limit([action+':account:'+username,action+':ip:'+ip],5 if action!='register' else 10):
                    raise ApiError(429,'尝试次数过多，请10分钟后重试')
                with app.connect() as c: u=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
                if action=='register':
                    pwd=self.password(payload.get('password'))
                    question,answer=self.answer(payload)
                    with app.connect() as c:
                        try:
                            uid=c.execute('INSERT INTO users(username,password,question,answer) VALUES (?,?,?,?)',(username,digest(pwd),question,digest(answer))).lastrowid
                        except sqlite3.IntegrityError: raise ApiError(409,'该账号无法注册，请换一个账号或登录')
                    return self.send({'ok':True,'user':{'username':username}},201,cookie=self.cookie(app.issue(uid)))
                if action=='login':
                    pwd=self.password(payload.get('password'))
                    good=matches(pwd,u['password'] if u else app.dummy)
                    if not u or not good: raise ApiError(401,'账号或密码错误')
                    with app.connect() as c: c.execute('DELETE FROM limits WHERE key=?',('login:account:'+username,))
                    return self.send({'ok':True,'user':{'username':username}},cookie=self.cookie(app.issue(u['id'])))
                question,answer=self.answer(payload)
                good=matches(answer,u['answer'] if u else app.dummy)
                if not u or not good or u['question']!=question: raise ApiError(401,'账号、安全问题或答案不匹配')
                token=secrets.token_urlsafe(32)
                with app.connect() as c:
                    c.execute('DELETE FROM resets WHERE expires<? OR user_id=?',(time.time(),u['id']))
                    c.execute('INSERT INTO resets VALUES (?,?,?)',(token_hash(token),u['id'],time.time()+300))
                self.send({'ok':True,'reset_token':token})
            except ApiError as e: self.send({'error':e.message},e.status)
            except (ValueError,TypeError,json.JSONDecodeError): self.send({'error':'请求格式错误'},400)
            except Exception:
                self.send({'error':'服务暂时不可用，请稍后重试'},500)
        def password(self,pwd):
            if not isinstance(pwd,str) or not 8<=len(pwd)<=128: raise ApiError(400,'密码长度需要8–128位')
            return pwd
        def answer(self,p):
            q,a=p.get('security_question_id'),p.get('security_answer')
            if q not in QUESTIONS or not isinstance(a,str) or not 1<=len(answer_text(a))<=128:
                raise ApiError(400,'请选择安全问题，答案长度需要1–128位')
            return q,answer_text(a)
        def reset(self,p):
            pwd=self.password(p.get('new_password'))
            token=p.get('reset_token')
            if not isinstance(token,str) or len(token)>128: raise ApiError(400,'重置凭证无效')
            hashed=digest(pwd)
            with app.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute('SELECT * FROM resets WHERE token=? AND expires>?',(token_hash(token),time.time())).fetchone()
                if not row: raise ApiError(401,'重置凭证已失效，请重新验证安全问题')
                uid=row['user_id']
                c.execute('UPDATE users SET password=? WHERE id=?',(hashed,uid))
                c.execute('DELETE FROM resets WHERE user_id=?',(uid,))
                c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            self.send({'ok':True},cookie=self.cookie(expire=True))
    return Handler

ACTIVE_STATES = ('starting','charging','stopping','unknown','ending')

class ChargingApp(CustomerAccounts):
    def __init__(self, db, page, insecure=True, balance_fen=100000):
        super().__init__(db,page,insecure)
        self.balance_fen=balance_fen
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS orders(
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,pile_no TEXT NOT NULL,gun_no INTEGER NOT NULL,
                serial_no TEXT UNIQUE NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                note TEXT NOT NULL DEFAULT '',transaction_json TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS occupied_gun ON orders(pile_no,gun_no)
                WHERE status IN ('starting','charging','stopping','unknown','ending');
            ''')
            c.execute("UPDATE orders SET status='unknown',note='服务重启，等待设备报文核实' WHERE status IN ('starting','stopping')")
    def active(self,pile,gun):
        with self.connect() as c:
            return c.execute("SELECT * FROM orders WHERE pile_no=? AND gun_no=? AND status IN ('starting','charging','stopping','unknown','ending')",(pile,gun)).fetchone()
    def owned(self,oid,uid):
        with self.connect() as c:
            row=c.execute('SELECT * FROM orders WHERE id=? AND user_id=?',(oid,uid)).fetchone()
        if not row: raise ApiError(404,'订单不存在')
        return row
    def device(self,pile,gun):
        sess=STORE.get_session(pile)
        with STORE._lock:
            entry=STORE.latest.get((pile,'%02d'%gun))
        if not sess: return None,{},'offline'
        if not entry or entry[0]<time.time()-90 or entry[0]<datetime.fromisoformat(sess.connected_at).timestamp():
            return sess,{},'unknown'
        data=dict(entry[1]);data['updated_at']=entry[0]
        if data.get('_truncated') or data.get('pile_no')!=pile: return sess,{},'unknown'
        if data.get('status')==1 or data.get('hw_fault'): state='fault'
        elif data.get('status')==3: state='charging'
        elif data.get('status')==2: state='idle'
        elif data.get('status')==0: state='offline'
        else: state='unknown'
        return sess,data,state
    def order_view(self,row):
        _,data,state=self.device(row['pile_no'],row['gun_no'])
        if data.get('serial_no')!=row['serial_no']: data={}
        return {'id':row['id'],'pile_no':row['pile_no'],'gun_no':row['gun_no'],
                'serial_no':row['serial_no'],'status':row['status'],'note':row['note'],
                'created_at':row['created'],'updated_at':row['updated'],'device_state':state,
                'latest':data,'transaction':json.loads(row['transaction_json']) if row['transaction_json'] else None}
    def customer_get(self,path,raw,uid):
        if path=='/api/customer/device':
            q=parse_qs(urlsplit(raw).query)
            pile,gun=self.target({'pile_no':q.get('pile_no',[''])[0],'gun_no':q.get('gun_no',[''])[0]})
            _,data,state=self.device(pile,gun)
            row=self.active(pile,gun)
            if row and row['user_id']!=uid:
                return {'availability':'occupied','message':'该枪正在使用中','pile_no':pile,'gun_no':gun}
            if row:
                return {'availability':'mine','order':self.order_view(row),'pile_no':pile,'gun_no':gun}
            if state=='charging': state='occupied'
            prices={k:{'electricity':v[0]/100000,'service':v[1]/100000} for k,v in CFG.rates.items()}
            return {'availability':state,'pile_no':pile,'gun_no':gun,'latest':data,
                    'billing':{'rates':prices,'periods':CFG.periods,'loss_ratio':CFG.loss_ratio},'payment_enabled':False}
        if path=='/api/customer/orders':
            with self.connect() as c:
                rows=c.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 100',(uid,)).fetchall()
            return {'orders':[self.order_view(r) for r in rows]}
        match=re.fullmatch(r'/api/customer/orders/([0-9a-f]{32})',path)
        if match: return {'order':self.order_view(self.owned(match[1],uid))}
        raise ApiError(404,'接口不存在')
    def target(self,p):
        pile=p.get('pile_no','');raw=p.get('gun_no','')
        if not isinstance(pile,str) or not re.fullmatch(r'\d{14}',pile) or not re.fullmatch(r'\d{1,2}',str(raw)):
            raise ApiError(400,'桩号或枪号无效')
        gun=int(raw)
        if not 1<=gun<=99: raise ApiError(400,'枪号无效')
        return pile,gun
    def customer_post(self,path,p,uid):
        if path=='/api/customer/orders':
            pile,gun=self.target(p)
            row=self.active(pile,gun)
            if row:
                if row['user_id']!=uid: raise ApiError(409,'该枪正在使用中')
                return {'order':self.order_view(row)}
            sess,_,_=self.device(pile,gun)
            if not sess: raise ApiError(409,'桩离线，无法启动')
            with sess.gun_locks[gun]:
                row=self.active(pile,gun)
                if row:
                    if row['user_id']!=uid: raise ApiError(409,'该枪正在使用中')
                    return {'order':self.order_view(row)}
                current,data,state=self.device(pile,gun)
                if current is not sess or state!='idle': raise ApiError(409,'设备不是可启动的空闲状态')
                if data.get('gun_plugged')!=1: raise ApiError(409,'请先插枪')
                # Use the original serial layout, with decimal BCD suffix and unique DB constraint.
                serial=(sess.pile_no_bcd+str_to_bcd(str(gun),1)+str_to_bcd(datetime.now().strftime('%y%m%d%H%M%S'),6)+str_to_bcd(str(secrets.randbelow(10000)),2))
                oid=secrets.token_hex(16);key=serial.hex().upper();now=time.time()
                try:
                    with self.connect() as c:
                        c.execute('INSERT INTO orders(id,user_id,pile_no,gun_no,serial_no,status,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                                  (oid,uid,pile,gun,key,'starting',now,now))
                except sqlite3.IntegrityError: raise ApiError(409,'设备订单已变更，请刷新后重试')
                try:
                    sess.dl_remote_start(gun,CFG.logic_card_no,'0',self.balance_fen,serial)
                    ev,box=sess.pending_start[key]
                    if not ev.wait(START_WAIT): self.set_unknown(key,'启动回复超时，等待设备确认')
                    # on-frame hook already updates the durable order, including very fast replies.
                except OSError: self.set_unknown(key,'命令发送中断，结果未知，禁止重复启动')
                finally: sess.pending_start.pop(key,None)
                return {'order':self.order_view(self.owned(oid,uid))}
        match=re.fullmatch(r'/api/customer/orders/([0-9a-f]{32})/stop',path)
        if not match: raise ApiError(404,'接口不存在')
        row=self.owned(match[1],uid)
        if row['status'] not in ACTIVE_STATES: return {'order':self.order_view(row)}
        gun=row['gun_no'];sess,_,_=self.device(row['pile_no'],gun)
        if not sess: raise ApiError(409,'设备离线，暂时无法停止；订单仍保留')
        with sess.gun_locks[gun]:
            row=self.owned(row['id'],uid)
            if row['status'] in ('stopping','ending'): return {'order':self.order_view(row)}
            if row['status'] not in ACTIVE_STATES: return {'order':self.order_view(row)}
            current,data,state=self.device(row['pile_no'],gun)
            if current is not sess or state!='charging' or data.get('serial_no')!=row['serial_no']:
                raise ApiError(409,'无法确认该枪正在执行本人订单，等待最新数据')
            with self.connect() as c:
                c.execute("UPDATE orders SET status='stopping',updated=?,note='' WHERE id=?",(time.time(),row['id']))
            pending_key = None
            try:
                pending_key = sess.dl_remote_stop(gun)
                event,box = sess.customer_stops[pending_key]
                if event.wait(STOP_WAIT):
                    if box.get('stop_result')!=1:
                        with self.connect() as c:
                            c.execute("UPDATE orders SET status='charging',updated=?,note='设备拒绝停机' WHERE id=? AND status='stopping'",(time.time(),row['id']))
                    # Success is only acceptance: keep reserved until transaction arrives.
                else: self.set_unknown(row['serial_no'],'停机回复超时，等待设备确认')
            except OSError: self.set_unknown(row['serial_no'],'停机发送中断，等待设备确认')
            finally:
                if pending_key is not None: sess.customer_stops.pop(pending_key,None)
            return {'order':self.order_view(self.owned(row['id'],uid))}
    def set_unknown(self,serial,note):
        with self.connect() as c:
            c.execute("UPDATE orders SET status='unknown',note=?,updated=? WHERE serial_no=? AND status IN ('starting','stopping')",(note,time.time(),serial))
    def start_reply(self,d):
        with self.connect() as c:
            c.execute("UPDATE orders SET status=?,note=?,updated=? WHERE serial_no=? AND pile_no=? AND gun_no=? AND status IN ('starting','unknown')",
                      ('charging' if d.get('start_result')==1 else 'failed','' if d.get('start_result')==1 else '启动失败，原因：'+str(d.get('fail_reason')),
                       time.time(),d.get('serial_no'),d.get('pile_no'),int(d.get('gun_no','0'))))
    def realtime(self,d):
        if d.get('_truncated'): return
        status=d.get('status');serial=d.get('serial_no')
        if status not in (2,3): return
        with self.connect() as c:
            if status==3:
                c.execute("UPDATE orders SET status='charging',note='',updated=? WHERE serial_no=? AND pile_no=? AND gun_no=? AND status IN ('starting','unknown')",(time.time(),serial,d.get('pile_no'),int(d.get('gun_no','0'))))
            else:
                c.execute("UPDATE orders SET status='ending',note='设备已空闲，等待最终交易记录',updated=? WHERE serial_no=? AND pile_no=? AND gun_no=? AND status IN ('charging','stopping','unknown')",(time.time(),serial,d.get('pile_no'),int(d.get('gun_no','0'))))
    def transaction(self,d):
        if d.get('_truncated'): raise ValueError('交易报文不完整，不能确认客户账单')
        with self.connect() as c:
            c.execute("UPDATE orders SET status='finished',note='设备交易已上报；未接支付扣款',transaction_json=?,updated=? WHERE serial_no=? AND pile_no=? AND gun_no=?",
                      (json.dumps(d,ensure_ascii=False),time.time(),d.get('serial_no'),d.get('pile_no'),int(d.get('gun_no','0'))))

class ReusableYkcTCPServer(ThreadingTCPServer):
    allow_reuse_address = True


def main():
    global CUSTOMER_APP
    ap = argparse.ArgumentParser(description="云快充平台协议 V1.6 平台端服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--web-port", type=int, default=8000,
                    help="Web/API/WebSocket 服务端口")
    ap.add_argument("--token", default="",
                    help="Web 接口静态访问令牌(可选), 前端用 ?token=xxx 或 Bearer 头携带")
    ap.add_argument("--admin-user", default="admin", help="管理员账号(默认 admin)")
    ap.add_argument("--admin-pass", default="123456", help="管理员密码(默认 123456)")
    ap.add_argument("--offline-timeout", type=int, default=90,
                    help="桩多少秒无数据判离线(默认90s,即3个30s心跳周期)")
    ap.add_argument("--customer-port", type=int, default=8088, help="客户端口；0禁用")
    ap.add_argument("--customer-page", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "charge.html"))
    ap.add_argument("--customer-db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "customer_data", "customers.sqlite3"))
    ap.add_argument("--customer-https", action="store_true", help="HTTPS反向代理模式，启用Secure Cookie")
    ap.add_argument("--customer-test-balance-fen", type=int, default=100000, help="联调下发额度(分)，不是客户充值或支付")
    args = ap.parse_args()
    if args.customer_port and len({args.port,args.web_port,args.customer_port}) != 3:
        ap.error("设备、管理、客户端口不能重复")
    if not 1 <= args.customer_test_balance_fen <= 0xFFFFFFFF:
        ap.error("联调额度无效")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler("ykc_server.log", encoding="utf-8")])

    server = ReusableYkcTCPServer((args.host, args.port), YkcHandler)
    server.daemon_threads = True
    log.info("云快充平台服务启动: %s:%d (充电桩TCP)", args.host, args.port)

    customer_http = None
    if args.customer_port:
        CUSTOMER_APP = ChargingApp(args.customer_db, args.customer_page,
                                   not args.customer_https, args.customer_test_balance_fen)
        customer_http = ThreadingHTTPServer(("0.0.0.0", args.customer_port), handler_for(CUSTOMER_APP))
        threading.Thread(target=customer_http.serve_forever, daemon=True).start()
        log.info("客户服务已启动: 端口%d HTTP=%s（联调额度，未接支付）", args.customer_port, not args.customer_https)

    web = WebApiServer("0.0.0.0", args.web_port, token=args.token,
                       credentials={args.admin_user: args.admin_pass})
    web.start()
    OfflineWatchdog(timeout=args.offline_timeout).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if customer_http:
            customer_http.shutdown()
            customer_http.server_close()
        server.server_close()
        log.info("服务已停止")


if __name__ == "__main__":
    main()
