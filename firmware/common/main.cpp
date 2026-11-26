#include "main.h"
#include "mailbox.hpp"
#include "can_bus.hpp"
#include "PID.h"
#include <algorithm>
#include <cmath>
#include <cstring>

#ifndef BENCH_BUILD_ID
#define BENCH_BUILD_ID 1U
#endif

namespace motor_test {
namespace {
#if defined(STM32F407xx)
constexpr uint32_t Board = 407;
constexpr uint32_t PortCount = 2;
#else
constexpr uint32_t Board = 723;
constexpr uint32_t PortCount = 3;
#endif
}

extern "C" {
extern const volatile uint32_t BenchIdentity[] __attribute__((section(".rodata.bench_identity"))) = {Magic, Protocol, Board, BENCH_BUILD_ID};
volatile uint32_t BenchCommand[CommandWords];
volatile uint32_t BenchHeartbeat;
volatile uint32_t BenchStatus[StatusWords];
volatile Discovery BenchDiscovery[DiscoverySize];
}

namespace {
constexpr uint32_t NoFeedback = 0xffffffffu;
constexpr float Pi = 3.14159265359f;
using std::clamp;

struct Route {
    uint32_t port = 1;
    uint32_t family = Rm;
    uint32_t model = 0;
    uint32_t id = 0;
    uint32_t tx = 0;
    uint32_t rx = 0;
    uint32_t slot = 0;
};

struct Motion {
    uint32_t mode;
    float target;
    float limit;
    float speedLimit;
    float kp;
    float ki;
    float positionKp;
    float ratio;
    bool reverse;
    uint32_t encoderRange;
    float temperatureLimit = 80;
    uint32_t feedbackTimeout = 100;
    float positionMin = -36000;
    float positionMax = 36000;
    bool positionLimited = false;
    float speedKd = 0;
    float positionKi = 0;
    float positionKd = 0;
    bool native = false;
    float ramp = 0; // 目标变化率，0 为不限
};

struct Feedback {
    uint32_t sequence;
    uint32_t tick;
    uint32_t lastEncoder;
    uint32_t error;
    uint32_t driveState;
    float speed;
    float angle;
    float zero;
    float effort;
    float temp;
    bool seen;
};

struct Transaction {
    uint32_t seq;
    uint32_t id;
    uint32_t expected[6];
    uint32_t deadline;
    uint32_t sendAt;
    uint32_t command;
    uint32_t phase;
    bool verify;
    bool sent;
};

Route route;
Motion motion{1, 0, 250, 100, 1, 1, 0.5f, 1, false, 65536};
Feedback feedback;
Transaction transaction;
PID_Controller_t speedPid, positionPid;
State state = Idle;
Result result = Ok, stopReason = Ok;
uint32_t ack, seenCommand, now, lastCycle, lastHeartbeat, heartbeatAt, lastSend, txFailedAt;
uint32_t scanFirst, scanLast, scanId, scanStepAt, scanStart, count, discoveryRevision;
uint32_t startAt, stopAt, stopStage, operateSeq, transactionResult[6], parameterCount;
uint32_t rangeIndex, dmMode;
uint32_t discoveredPort, discoveredFamily;
uint32_t stageAt, lkAngleRequestAt;
double lkAngleOffset;
bool preferDmBase, enableIssued, lastEnableViaBase, pollModeNext;
enum StartStage { EnableDrive, PreloadPosition, SetDmMode, VerifyDmMode, QueryLkAngle, WaitLkAngle };
StartStage startStage;
float dmRanges[3];
float output;
float setpoint; // 斜坡后实际给定
float speedReference; // 速度环给定
bool selected, stopAwaitingAck, disabledAck, hostHasPulse, flashReady;
bool lkZeroNeedsReload;

float F32(uint32_t bits) {
    float value;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

uint32_t Bits(float value) {
    uint32_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

uint32_t Little16(const uint8_t* bytes) { return bytes[0] | (uint32_t(bytes[1]) << 8); }
uint32_t Big16(const uint8_t* bytes) { return (uint32_t(bytes[0]) << 8) | bytes[1]; }
uint32_t Little32(const uint8_t* b) { return Little16(b) | (Little16(b + 2) << 16); }

void PutLittle32(uint8_t* bytes, uint32_t value) {
    for (unsigned i = 0; i < 4; ++i) bytes[i] = (value >> (8 * i)) & 0xff;
}

bool InRange(float f, float lo, float hi) { return std::isfinite(f) && f >= lo && f <= hi; }

float Approach(float from, float to, float step) {
    return from < to ? std::min(from + step, to) : std::max(from - step, to);
}

void Finish(uint32_t seq, Result value) {
    ack = seq;
    result = value;
}

bool Fresh() { return feedback.seen && now - feedback.tick < motion.feedbackTimeout; }
bool HostAlive() { return hostHasPulse && now - heartbeatAt < 350; }
bool DmCalibrated() { return rangeIndex == 4 && dmRanges[0] > 0 && dmRanges[1] > 0 && dmRanges[2] > 0; }
bool HasTemperature() { return route.family != Rm || route.model != 1; }

float Position(const Motion& settings) {
    return (feedback.angle - feedback.zero) * (settings.reverse ? -1.0f : 1.0f) / settings.ratio;
}

uint32_t DriveMode(const Motion& settings) {
    return !settings.native ? 1 : settings.mode == 1 ? 3 : 2;
}

uint32_t DriveTx(uint32_t mode) {
    return mode >= 1 && mode <= 4 ? route.tx + (mode - 1) * 0x100 : route.tx;
}

bool DmRouteUsable(uint32_t mode) {
    const uint32_t tx = DriveTx(mode);
    if (tx >= 0x7ff || tx == route.rx) return false;
    if (tx == route.tx || route.port != discoveredPort || discoveredFamily != Dm) return true;
    for (unsigned i = 0; i < count; ++i) {
        if (BenchDiscovery[i].id == tx || BenchDiscovery[i].rx == tx) return false;
    }
    return true;
}

bool ValidModel(uint32_t family, uint32_t model) {
    switch (family) {
    case Rm: return model < 3;
    case Dm: return model < 10;
    case Lk: return model < 9;
    default: return false;
    }
}

double NativeAngle(const Motion& settings, float target) {
    return double(feedback.zero) + double(target) * settings.ratio * (settings.reverse ? -1 : 1);
}

bool NativeTargetAllowed(const Motion& settings, float target, bool hasLkOrigin) {
    if (!settings.native) return true;
    if (route.family == Dm) {
        if (settings.mode == 1) return double(std::abs(target)) * settings.ratio * (Pi / 30) <= dmRanges[1];
        return std::abs(NativeAngle(settings, target)) * (Pi / 180) <= dmRanges[0];
    }
    if (settings.mode == 1) return true;
    const double dps = double(settings.speedLimit) * settings.ratio * 6;
    const double counts = (NativeAngle(settings, target) + lkAngleOffset) * 100;
    return dps >= 1 && dps <= 65535 &&
           (!hasLkOrigin || (counts >= INT32_MIN && counts <= INT32_MAX));
}

bool RmRoute(uint32_t model, uint32_t rx, uint32_t& id, uint32_t& tx, uint32_t& slot) {
    const uint32_t base = model == 2 ? 0x204 : 0x200;
    const uint32_t limit = model == 2 ? 7 : 8;
    if (rx <= base || rx > base + limit) return false;
    id = rx - base;
    slot = (id - 1) % 4;
    tx = model == 2 ? (id <= 4 ? 0x1ff : 0x2ff) : (id <= 4 ? 0x200 : 0x1ff);
    return true;
}

void Record(uint32_t id, uint32_t rx, uint32_t tx, uint32_t slot, uint32_t tick) {
    const auto flags = slot + 1;
    for (unsigned i = 0; i < count; ++i) {
        auto& entry = BenchDiscovery[i];
        if (entry.id == id && entry.rx == rx) {
            entry.lastSeen = tick;
            return;
        }
        if (route.family == Dm && (entry.id == id || entry.rx == rx)) {
            entry.flags = entry.flags | 0x100;
            ++discoveryRevision;
        }
    }
    if (count >= DiscoverySize) return;
    auto& entry = BenchDiscovery[count++];
    entry.id = id;
    entry.rx = rx;
    entry.tx = tx;
    entry.flags = flags;
    if (route.family == Dm) {
        for (unsigned i = 0; i + 1 < count; ++i) {
            if (BenchDiscovery[i].id == id || BenchDiscovery[i].rx == rx) entry.flags = flags | 0x100;
        }
    }
    entry.lastSeen = tick;
    ++discoveryRevision;
}

bool DmRegisterReply(const Frame& f, uint32_t id, uint32_t op, uint32_t reg) {
    return Little16(f.bytes) == id && f.bytes[2] == op && f.bytes[3] == reg;
}

bool DmQuery(uint32_t id, uint8_t op, uint8_t reg = 0, uint32_t data = 0) {
    uint8_t bytes[8] = {uint8_t(id), uint8_t(id >> 8), op, reg};
    PutLittle32(bytes + 4, data);
    return Send(0x7ff, bytes);
}

bool LkQuery(uint8_t op, uint32_t id) {
    const uint8_t data[8] = {op};
    return Send(id, data);
}

void ScanFrame(const Frame& f) {
    uint32_t id, tx, slot;
    if (route.family == Rm && Big16(f.bytes) < 8192 && RmRoute(route.model, f.id, id, tx, slot)) {
        Record(id, f.id, tx, slot, f.tick);
    }
    if (route.family == Lk && scanId >= scanFirst && scanId <= scanLast &&
        f.id == 0x140 + scanId && f.bytes[0] == 0x9c) {
        Record(scanId, f.id, f.id, 0, f.tick);
    }
    if (route.family == Dm && scanId >= scanFirst && scanId <= scanLast &&
        DmRegisterReply(f, scanId, 0x33, 8) && Little32(f.bytes + 4) == scanId) {
        Record(scanId, f.id, scanId, 0, f.tick);
    }
}

void ClearFeedback() {
    feedback = {};
    feedback.tick = now;
    speedPid = {};
    positionPid = {};
    rangeIndex = dmMode = 0;
    lkAngleOffset = 0;
    preferDmBase = false;
    std::fill_n(dmRanges, 3, 0);
}

void UpdateAngle(uint32_t encoder, uint32_t range, uint32_t tick) {
    if (feedback.seen && tick - feedback.tick < motion.feedbackTimeout) {
        int32_t delta = int32_t(encoder) - int32_t(feedback.lastEncoder);
        if (delta > int32_t(range / 2)) delta -= int32_t(range);
        if (delta < -int32_t(range / 2)) delta += int32_t(range);
        feedback.angle += float(delta) * (360.0f / float(range));
    } else {
        feedback.angle = feedback.zero = 0;
    }
    feedback.lastEncoder = encoder;
}

float Decode(uint32_t raw, uint32_t max, float limit) {
    return (2.0f * float(raw) / float(max) - 1.0f) * limit;
}

uint32_t Encode(float value, uint32_t max, float limit) {
    return uint32_t(std::lround((clamp(value / limit, -1.0f, 1.0f) + 1.0f) * float(max) * 0.5f));
}

void ParameterReply(const Frame& f) {
    auto& p = transaction;
    if (!p.seq || !p.sent) return;
    const uint32_t op = p.phase == 2 ? (route.family == Dm ? 0x33 : p.id) : p.command;
    if (route.family == Dm) {
        if (p.command == 0xaa && p.phase == 1) {
            if (Little16(f.bytes) != route.id || f.bytes[2] != 0xaa) return;
        } else if (!DmRegisterReply(f, route.id, op, p.id)) {
            return;
        }
    } else if (f.bytes[0] != op) {
        return;
    }
    if (p.phase == 1 && p.verify) {
        p.phase = 2;
        p.sent = false;
        p.sendAt = now + (p.command == 0x32 ? 50 : 3);
        return;
    }
    if (route.family == Lk && p.id == 0x30) {
        parameterCount = 6;
        for (unsigned i = 0; i < 6; ++i) transactionResult[i] = f.bytes[2 + i];
    } else {
        parameterCount = p.command == 0xaa ? 0 : 1;
        transactionResult[0] = route.family == Lk && (p.command == 0x19 || p.id == 0x90)
            ? Little16(f.bytes + 6) : Little32(f.bytes + 4);
    }
    bool equal = true;
    for (unsigned i = 0; p.verify && i < parameterCount; ++i) equal &= p.expected[i] == transactionResult[i];
    Finish(p.seq, equal ? Ok : ReadbackMismatch);
    if (route.family == Dm && p.id == 10 && p.command == 0x55) rangeIndex = 0;
    p = {};
}

void SelectedFrame(const Frame& f) {
    if (f.id != route.rx) return;
    if (route.family == Dm && f.bytes[2] == 0x33 && Little16(f.bytes) == route.id) {
        const uint32_t reg = f.bytes[3];
        if (reg == 10 && state == Starting && startStage == VerifyDmMode) {
            dmMode = Little32(f.bytes + 4);
            if (dmMode == DriveMode(motion)) {
                startStage = motion.native && motion.mode == 2 ? PreloadPosition : EnableDrive;
                stageAt = now;
            }
            return;
        }
        if (rangeIndex < 4 && reg == (rangeIndex < 3 ? rangeIndex + 21 : 10)) {
            if (rangeIndex == 3) {
                dmMode = Little32(f.bytes + 4);
                ++rangeIndex;
            } else {
                const float value = F32(Little32(f.bytes + 4));
                if (InRange(value, 0.001f, 10000)) dmRanges[rangeIndex++] = value;
            }
            return;
        }
        ParameterReply(f);
        return;
    }
    if (route.family == Dm && Little16(f.bytes) == route.id &&
        (f.bytes[2] == 0x55 || f.bytes[2] == 0xaa)) {
        ParameterReply(f);
        return;
    }
    if (route.family == Rm) {
        const auto encoder = Big16(f.bytes);
        if (encoder >= 8192) return;
        UpdateAngle(encoder, 8192, f.tick);
        feedback.speed = float(int16_t(Big16(f.bytes + 2)));
        feedback.effort = float(int16_t(Big16(f.bytes + 4)));
        feedback.temp = HasTemperature() ? f.bytes[6] : 0;
    } else if (route.family == Lk) {
        switch (f.bytes[0]) {
        case 0x88:
            if (state == Starting && startStage == EnableDrive && enableIssued) {
                state = Running;
                txFailedAt = now;
            }
            return;
        case 0x80:
            if (state == Stopping) disabledAck = true;
            return;
        case 0x92:
            if (state == Starting && startStage == WaitLkAngle && int32_t(f.tick - lkAngleRequestAt) >= 0) {
                uint64_t raw = 0;
                for (unsigned i = 0; i < 7; ++i) raw |= uint64_t(f.bytes[i + 1]) << (i * 8);
                if (raw & (uint64_t(1) << 55)) raw |= uint64_t(0xff) << 56;
                int64_t counts;
                std::memcpy(&counts, &raw, sizeof(counts));
                lkAngleOffset = double(counts) * 0.01 - feedback.angle;
                startStage = EnableDrive;
                stageAt = now;
            }
            return;
        case 0x9c: case 0xa1: case 0xa2: case 0xa4:
            break;
        case 0x9a:
            feedback.error = f.bytes[7];
            return;
        default:
            ParameterReply(f);
            return;
        }
        const auto encoder = Little16(f.bytes + 6);
        if (encoder >= motion.encoderRange) return;
        UpdateAngle(encoder, motion.encoderRange, f.tick);
        feedback.speed = float(int16_t(Little16(f.bytes + 4))) / 6;
        feedback.effort = float(int16_t(Little16(f.bytes + 2)));
        feedback.temp = float(int8_t(f.bytes[1]));
    } else {
        // MIT feedback carries the low four bits of the motor ID and a four-bit state.
        if ((f.bytes[0] & 0xf) != (route.id & 0xf) || !DmCalibrated()) return;
        feedback.driveState = f.bytes[0] >> 4;
        feedback.error = feedback.driveState >= 8 ? feedback.driveState : 0;
        if (state == Stopping && feedback.driveState == 0) disabledAck = true;
        const float angle = Decode(Big16(f.bytes + 1), 65535, dmRanges[0]) * (180 / Pi);
        if (!feedback.seen) feedback.zero = angle;
        feedback.angle = angle;
        feedback.speed = Decode((uint32_t(f.bytes[3]) << 4) | (f.bytes[4] >> 4), 4095, dmRanges[1]) * (30 / Pi);
        feedback.effort = Decode(((uint32_t(f.bytes[4]) & 15) << 8) | f.bytes[5], 4095, dmRanges[2]);
        feedback.temp = float(std::max(f.bytes[6], f.bytes[7]));
        auto& p = transaction;
        if (p.seq && p.command == 0xfe && p.sent && p.phase == 2 && int32_t(f.tick - p.expected[0]) >= 0 &&
            feedback.driveState == 0 && std::abs(feedback.angle) <= 1.0f && std::abs(feedback.speed) <= 1.0f) {
            feedback.zero = feedback.angle;
            Finish(p.seq, Ok);
            p = {};
        }
    }
    feedback.tick = f.tick;
    feedback.seen = true;
    ++feedback.sequence;
}

void BeginStop(Result reason, uint32_t seq) {
    if (state == Stopping) {
        if (seq) operateSeq = seq;
        return;
    }
    const bool dmEnabled = selected && route.family == Dm && Fresh() && feedback.driveState == 1;
    if (state == Starting && !enableIssued && !dmEnabled) {
        Abort();
        if (route.family == Dm) rangeIndex = 0;
        state = reason == Ok ? Idle : Fault;
        operateSeq = 0;
        if (seq) Finish(seq, reason);
        else result = reason;
        return;
    }
    if (state != Running && state != Starting && !dmEnabled) {
        if (state == Scanning || transaction.seq) Abort();
        state = Idle;
        transaction = {};
        if (seq) Finish(seq, Ok);
        return;
    }
    state = Stopping;
    stopReason = reason;
    operateSeq = seq;
    stopAt = lastSend = now;
    stopStage = 0;
    output = 0;
    disabledAck = false;
    stopAwaitingAck = route.family != Rm;
    PID_Reset(&speedPid);
    PID_Reset(&positionPid);
    Abort();
}

bool Special(uint8_t op, bool useBase = false) {
    if (route.family == Lk) return LkQuery(op, route.tx);
    const uint8_t data[8] = {255, 255, 255, 255, 255, 255, 255, op};
    return Send(useBase ? route.tx : DriveTx(dmMode), data);
}

bool SendOutput(float value) {
    uint8_t data[8]{};
    if (route.family == Rm) {
        const auto raw = uint16_t(int16_t(value));
        data[route.slot * 2] = raw >> 8;
        data[route.slot * 2 + 1] = raw & 0xff;
    } else if (route.family == Lk) {
        data[0] = 0xa1;
        const auto raw = uint16_t(int16_t(value));
        data[4] = raw & 0xff;
        data[5] = raw >> 8;
    } else {
        const auto p = Encode(0, 65535, dmRanges[0]);
        const auto v = Encode(0, 4095, dmRanges[1]);
        const auto t = Encode(value, 4095, dmRanges[2]);
        data[0] = p >> 8;
        data[1] = p & 0xff;
        data[2] = v >> 4;
        data[3] = (v & 15) << 4;
        data[6] = t >> 8;
        data[7] = t & 0xff;
    }
    return Send(route.tx, data);
}

bool SendNative(float target) {
    uint8_t data[8]{};
    const float sign = motion.reverse ? -1.0f : 1.0f;
    const float motorSpeed = target * motion.ratio * sign;
    const float maxSpeed = motion.speedLimit * motion.ratio;
    if (route.family == Dm) {
        if (motion.mode == 1) {
            PutLittle32(data, Bits(motorSpeed * (Pi / 30)));
            return Send(DriveTx(dmMode), data, 4);
        }
        PutLittle32(data, Bits(float(NativeAngle(motion, target) * (Pi / 180))));
        PutLittle32(data + 4, Bits(std::min(maxSpeed * (Pi / 30), dmRanges[1])));
        return Send(DriveTx(dmMode), data);
    }
    if (motion.mode == 1) {
        data[0] = 0xa2;
        PutLittle32(data + 4, uint32_t(int32_t(std::llround(double(motorSpeed) * 600))));
    } else {
        data[0] = 0xa4;
        const auto dps = uint16_t(std::lround(double(maxSpeed) * 6));
        data[2] = uint8_t(dps);
        data[3] = uint8_t(dps >> 8);
        const auto counts = int32_t(std::llround((NativeAngle(motion, target) + lkAngleOffset) * 100));
        PutLittle32(data + 4, uint32_t(counts));
    }
    return Send(route.tx, data);
}

void ServiceStop() {
    if (now - lastSend < 10) return;
    lastSend = now;
    if (Pending() || BusOff()) {
        Abort();
        Recover();
        return;
    }
    if (stopStage && (stopAwaitingAck ? disabledAck : Transmitted())) {
        state = stopReason == Ok ? Idle : Fault;
        result = stopReason;
        if (operateSeq) Finish(operateSeq, stopReason);
        operateSeq = 0;
        return;
    }
    if (now - stopAt > 250) result = Timeout;
    if (route.family == Rm) stopStage |= SendOutput(0);
    if (route.family == Lk) stopStage |= Special(0x80);
    if (route.family == Dm) {
        const bool tryBase = ((now - stopAt) / 10 % 2 == 0) != preferDmBase;
        stopStage |= Special(0xfd, tryBase);
    }
}

void ServiceScan() {
    if (!HostAlive()) {
        Abort();
        state = Idle;
        Finish(operateSeq, HostLost);
        return;
    }
    if (route.family == Rm) {
        if (now - scanStart < 350) return;
    } else {
        if (now - scanStepAt < 16) return;
        if (Pending()) {
            Abort();
            return;
        }
        if (scanId == NoFeedback) scanId = scanFirst;
        else ++scanId;
        if (scanId <= scanLast) {
            scanStepAt = now;
            if (route.family == Lk) LkQuery(0x9c, 0x140 + scanId);
            else DmQuery(scanId, 0x33, 8);
            return;
        }
    }
    state = Idle;
    Finish(operateSeq, BusOff() ? CanFailure : Ok);
}

bool ParamAllowed(uint32_t id, uint32_t bits, bool write) {
    if (route.family == Lk) {
        return id == 0x30 || (id == 0x90 && (!write || (motion.encoderRange == 16384 && bits < 16384))) ||
               (id == 0x33 && (!write || (bits >= 1 && bits <= 1000000)));
    }
    if (route.family != Dm) return false;
    switch (id) {
    case 2:
        return !write || InRange(F32(bits), 80, 199);
    case 7: case 8: case 14: case 21: case 22: case 23: case 60:
        return !write;
    case 9:
        return !write || (bits >= 10 && bits <= 1000000);
    case 10:
        return !write || (bits >= 1 && bits <= 3);
    case 4: case 6: case 25: case 26: case 27: case 28:
        return !write || InRange(F32(bits), id < 10 ? 0.001f : 0, 10000);
    case 5:
        return !write || InRange(F32(bits), -10000, -0.001f);
    case 24:
        return !write || InRange(F32(bits), 100, 10000);
    case 31:
        return !write || InRange(F32(bits), 1, 30);
    case 32:
        return !write || InRange(F32(bits), 0.001f, 499.999f);
    default:
        return false;
    }
}

void BeginParam(uint32_t seq, uint32_t op, const uint32_t* words) {
    if (!selected || !Fresh()) { Finish(seq, Offline); return; }
    const auto id = words[8];
    if (route.family == Lk && op == WriteParam && id == 0x90) {
        if (lkZeroNeedsReload) { Finish(seq, ReloadZero); return; }
        if (std::abs(feedback.speed) > 1.0f) { Finish(seq, MotorNotStill); return; }
    }
    if (op == SaveParams) {
        if (route.family == Rm || (route.family == Lk && id != 0x30)) { Finish(seq, Invalid); return; }
    } else if (!ParamAllowed(id, words[9], op == WriteParam)) {
        Finish(seq, Invalid);
        return;
    }
    transaction = {};
    auto& p = transaction;
    p.seq = seq;
    p.id = id;
    p.deadline = now + 900;
    p.sendAt = now;
    for (unsigned i = 0; i < 6; ++i) p.expected[i] = words[9 + i];
    if (route.family == Lk && id == 0x30 &&
        std::any_of(p.expected, p.expected + 6, [](auto value) { return value > 255; })) {
        p = {};
        Finish(seq, Invalid);
        return;
    }
    p.verify = op == WriteParam || (op == SaveParams && route.family == Lk);
    p.phase = 1;
    if (route.family == Dm) p.command = op == SaveParams ? 0xaa : (op == WriteParam ? 0x55 : 0x33);
    else p.command = op == SaveParams ? 0x32 : (op == WriteParam ? id + 1 : id);
}

void ServiceParam() {
    auto& p = transaction;
    if (!HostAlive() || int32_t(now - p.deadline) >= 0 || BusOff()) {
        Abort();
        Finish(p.seq, !HostAlive() ? HostLost : (BusOff() ? CanFailure : Timeout));
        p = {};
        return;
    }
    if (int32_t(now - p.sendAt) < 0 || Pending()) return;
    if (p.command == 0xfe) {
        if (!p.sent && Special(0xfe)) {
            p.sent = true;
            p.sendAt = now + 100;
        } else if (p.sent && DmQuery(route.id, 0xcc)) {
            if (p.phase != 2) p.expected[0] = now;
            p.phase = 2;
            p.sendAt = now + 30;
        }
        return;
    }
    if (p.sent) return;
    if (route.family == Dm) {
        const auto op = uint8_t(p.phase == 2 ? 0x33 : p.command);
        p.sent = DmQuery(route.id, op, uint8_t(op == 0xaa ? 0 : p.id), op == 0x55 ? p.expected[0] : 0);
    } else {
        const auto op = uint8_t(p.phase == 2 ? p.id : p.command);
        uint8_t bytes[8] = {op};
        if (op == 0x31 || op == 0x32) {
            for (unsigned i = 0; i < 6; ++i) bytes[i + 2] = uint8_t(p.expected[i]);
        }
        if (op == 0x34) PutLittle32(bytes + 4, p.expected[0]);
        if (op == 0x91) {
            bytes[6] = uint8_t(p.expected[0]);
            bytes[7] = uint8_t(p.expected[0] >> 8);
        }
        p.sent = Send(route.tx, bytes);
        if (p.sent && (op == 0x19 || op == 0x91)) lkZeroNeedsReload = true;
    }
}

bool ValidRoute(const Route& candidate) {
    if (candidate.port == 0 || candidate.port > PortCount || !ValidModel(candidate.family, candidate.model) ||
        candidate.tx > 0x7ff || candidate.rx > 0x7ff) return false;
    if (candidate.family == Rm) {
        uint32_t id, tx, slot;
        return RmRoute(candidate.model, candidate.rx, id, tx, slot) &&
               candidate.id == id && candidate.tx == tx && candidate.slot == slot;
    }
    if (candidate.family == Lk) return candidate.id >= 1 && candidate.id <= 32 &&
                                      candidate.tx >= 0x141 && candidate.tx <= 0x160 && candidate.rx >= 0x141 && candidate.rx <= 0x160;
    return candidate.id >= 1 && candidate.id < 0x7ff &&
           candidate.tx == candidate.id && candidate.rx != 0x7ff && candidate.rx != candidate.tx;
}

float MaximumOutput() {
    if (route.family == Lk) return 2048;
    if (route.family == Dm) return std::min(dmRanges[2], 20.0f);
    if (route.model == 0) return 16384;
    if (route.model == 1) return 10000;
    return 30000;
}

void Command(const uint32_t* words) {
    const auto seq = words[0], op = words[1];
    parameterCount = 0;
    if (op == Stop) { BeginStop(Ok, seq); return; }
    if (!HostAlive()) { Finish(seq, HostLost); return; }
    if (op == Target) {
        const float value = F32(words[9]);
        const float limit = motion.mode == 1 ? motion.speedLimit : 36000.0f;
        if (state != Running || !InRange(value, -limit, limit)) { Finish(seq, Invalid); return; }
        if (motion.mode == 2 && motion.positionLimited && !InRange(value, motion.positionMin, motion.positionMax)) {
            Finish(seq, PositionLimit);
            return;
        }
        if (!NativeTargetAllowed(motion, value, true)) { Finish(seq, DriveRange); return; }
        motion.target = value;
        Finish(seq, Ok);
        return;
    }
    if (op == Tune) {
        // 运行中在线改软件 PID、限幅与斜坡；积分项与输出同单位，改 Ki 无跳变。
        const float kp = F32(words[8]), ki = F32(words[9]), kd = F32(words[10]);
        const float positionKp = F32(words[11]), positionKi = F32(words[12]), positionKd = F32(words[13]);
        const float limit = F32(words[14]), speedLimit = F32(words[15]), ramp = F32(words[28]);
        if (state != Running || motion.native || !InRange(kp, 0, 100) || !InRange(ki, 0, 100) ||
            !InRange(kd, 0, 100) || !InRange(positionKp, 0, 30) || !InRange(positionKi, 0, 100) ||
            !InRange(positionKd, 0, 100) || !InRange(limit, 0.001f, MaximumOutput()) ||
            !InRange(speedLimit, 0.1f, 3000) || !InRange(ramp, 0, 1e6f) ||
            (motion.mode == 1 && !InRange(motion.target, -speedLimit, speedLimit))) {
            Finish(seq, Invalid);
            return;
        }
        motion.kp = kp;
        motion.ki = ki;
        motion.speedKd = kd;
        motion.positionKp = positionKp;
        motion.positionKi = positionKi;
        motion.positionKd = positionKd;
        motion.limit = limit;
        motion.speedLimit = speedLimit;
        motion.ramp = ramp;
        PID_UpdateParameters(&speedPid, kp, ki, kd, limit, limit, 0.005f);
        PID_UpdateParameters(&positionPid, positionKp, positionKi, positionKd, speedLimit, speedLimit, 0.005f);
        Finish(seq, Ok);
        return;
    }
    if ((state != Idle && state != Fault) || transaction.seq) { Finish(seq, Busy); return; }
    const Route candidate{words[2], words[3], words[4], words[5], words[6], words[7], words[18]};
    if (op == Scan) {
        const uint32_t maxId = candidate.family == Dm ? 0x7fe : 32;
        if (candidate.port == 0 || candidate.port > PortCount || !ValidModel(candidate.family, candidate.model) ||
            words[8] < 1 || words[8] > words[9] || words[9] > maxId) {
            Finish(seq, Invalid);
            return;
        }
        if (Pending()) { Abort(); Finish(seq, Busy); return; }
        selected = false;
        route = candidate;
        SelectPort(route.port);
        discoveredPort = candidate.port;
        discoveredFamily = candidate.family;
        count = 0;
        ++discoveryRevision;
        scanFirst = words[8];
        scanLast = words[9];
        scanId = NoFeedback;
        scanStart = scanStepAt = now;
        operateSeq = seq;
        state = Scanning;
        ClearFeedback();
        return;
    }
    if (op == Select) {
        if (!ValidRoute(candidate)) { Finish(seq, Invalid); return; }
        if (Pending()) { Abort(); Finish(seq, Busy); return; }
        if (discoveredPort == candidate.port && discoveredFamily == candidate.family) {
            for (unsigned i = 0; i < count; ++i) {
                if ((BenchDiscovery[i].id == candidate.id || BenchDiscovery[i].rx == candidate.rx) &&
                    (BenchDiscovery[i].flags & 0x100)) {
                    Finish(seq, Conflict);
                    return;
                }
            }
        }
        route = candidate;
        SelectPort(route.port);
        ClearFeedback();
        motion.ratio = 1;
        motion.reverse = false;
        motion.encoderRange = 65536;
        motion.feedbackTimeout = 100;
        lkZeroNeedsReload = false;
        if (candidate.family == Lk) {
            if (words[17] == 16384 || words[17] == 32768 || words[17] == 65536) motion.encoderRange = words[17];
        }
        selected = true;
        state = Idle;
        lastSend = now;
        Finish(seq, Ok);
        return;
    }
    if (op == ReadParam || op == WriteParam || op == SaveParams) { BeginParam(seq, op, words); return; }
    if (!selected || !Fresh()) { Finish(seq, Offline); return; }
    if (op == Zero) {
        const float position = F32(words[8]), ratio = F32(words[9]);
        if (!InRange(position, -36000, 36000) || !InRange(ratio, 0.01f, 1000)) {
            Finish(seq, Invalid);
            return;
        }
        motion.ratio = ratio;
        motion.reverse = words[10] != 0;
        feedback.zero = feedback.angle - position * ratio * (motion.reverse ? -1 : 1);
        Finish(seq, Ok);
        return;
    }
    if (op == NativeZero) {
        if (route.family == Rm) { Finish(seq, Invalid); return; }
        if (std::abs(feedback.speed) > 1.0f || (route.family == Dm && feedback.driveState != 0)) {
            Finish(seq, MotorNotStill);
            return;
        }
        if (route.family == Dm && (!DmCalibrated() || dmMode < 1 || dmMode > 4 || !DmRouteUsable(dmMode))) {
            Finish(seq, WrongMode);
            return;
        }
        if (lkZeroNeedsReload) { Finish(seq, ReloadZero); return; }
        transaction = {};
        transaction.seq = seq;
        transaction.command = route.family == Dm ? 0xfe : 0x19;
        transaction.phase = 1;
        transaction.sendAt = now;
        transaction.deadline = now + 900;
        return;
    }
    if (op != Start) { Finish(seq, Invalid); return; }
    if (route.family == Lk && lkZeroNeedsReload) { Finish(seq, ReloadZero); return; }
    if (BusOff() || feedback.error) { Finish(seq, BusOff() ? CanFailure : MotorFault); return; }
    if (route.family == Dm && !DmCalibrated()) { Finish(seq, WrongMode); return; }
    const Motion next{words[8], F32(words[9]), F32(words[10]), F32(words[11]), F32(words[12]),
                      F32(words[13]), F32(words[14]), F32(words[15]), words[16] != 0, motion.encoderRange,
                      F32(words[19]), words[20], F32(words[21]), F32(words[22]), words[23] != 0,
                      F32(words[24]), F32(words[25]), F32(words[26]), words[27] != 0, F32(words[28])};
    if ((next.mode != 1 && next.mode != 2) || !InRange(next.speedLimit, 0.1f, 3000) ||
        !InRange(next.target, next.mode == 1 ? -next.speedLimit : -36000,
                next.mode == 1 ? next.speedLimit : 36000) ||
        (next.native && route.family == Rm) ||
        (!next.native && !InRange(next.limit, 0.001f, MaximumOutput())) || !InRange(next.kp, 0, 100) ||
        !InRange(next.ki, 0, 100) || !InRange(next.positionKp, 0, 30) || !InRange(next.ratio, 0.01f, 1000) ||
        !InRange(next.speedKd, 0, 100) || !InRange(next.positionKi, 0, 100) || !InRange(next.positionKd, 0, 100) ||
        !InRange(next.ramp, 0, 1e6f) || !InRange(next.temperatureLimit, 20, 150) || next.feedbackTimeout < 25 || next.feedbackTimeout > 250 ||
        !InRange(next.positionMin, -36000, 36000) || !InRange(next.positionMax, -36000, 36000) ||
        (next.positionLimited && next.positionMin >= next.positionMax)) {
        Finish(seq, Invalid);
        return;
    }
    if (HasTemperature() && feedback.temp >= next.temperatureLimit) { Finish(seq, OverTemperature); return; }
    if (now - feedback.tick >= next.feedbackTimeout) { Finish(seq, Offline); return; }
    if (next.positionLimited && (!InRange(Position(next), next.positionMin, next.positionMax) ||
        (next.mode == 2 && !InRange(next.target, next.positionMin, next.positionMax)))) {
        Finish(seq, PositionLimit);
        return;
    }
    if (route.family == Dm) {
        if (!DmRouteUsable(DriveMode(next))) { Finish(seq, Conflict); return; }
        if (feedback.driveState != 0 || (dmMode != DriveMode(next) && std::abs(feedback.speed) > 1.0f)) {
            Finish(seq, MotorNotStill);
            return;
        }
    }
    if (!NativeTargetAllowed(next, next.target, false)) { Finish(seq, DriveRange); return; }
    motion = next;
    PID_Init(&speedPid, motion.kp, motion.ki, motion.speedKd, motion.limit, motion.limit, 0.005f);
    PID_Init(&positionPid, motion.positionKp, motion.positionKi, motion.positionKd,
             motion.speedLimit, motion.speedLimit, 0.005f);
    // 有斜坡时从当前测量值出发，避免使能瞬间给定突变。
    const float measuredSpeed = feedback.speed * (motion.reverse ? -1 : 1) / motion.ratio;
    setpoint = motion.ramp <= 0 ? motion.target
             : (motion.mode == 1 ? clamp(measuredSpeed, -motion.speedLimit, motion.speedLimit) : Position(motion));
    speedReference = 0;
    operateSeq = seq;
    startAt = lastSend = now;
    stageAt = now;
    enableIssued = lastEnableViaBase = pollModeNext = false;
    startStage = EnableDrive;
    if (route.family == Dm && dmMode != DriveMode(motion)) startStage = SetDmMode;
    else if (motion.native && motion.mode == 2) startStage = route.family == Dm ? PreloadPosition : QueryLkAngle;
    txFailedAt = now;
    state = route.family == Rm ? Running : Starting;
    if (state == Running) { Finish(seq, Ok); operateSeq = 0; }
}

void Monitor() {
    if (now - lastSend < (route.family == Lk ? 10u : 15u) || Pending()) return;
    lastSend = now;
    if (route.family == Lk) LkQuery(0x9c, route.tx);
    if (route.family == Dm) {
        if (rangeIndex < 4) DmQuery(route.id, 0x33, uint8_t(rangeIndex < 3 ? rangeIndex + 21 : 10));
        else DmQuery(route.id, 0xcc);
    }
}

void ServiceStarting() {
    if (now - startAt > 650 || (startStage == EnableDrive && now - stageAt > 100)) {
        BeginStop(Timeout, operateSeq);
        return;
    }
    if (route.family == Dm && startStage == EnableDrive && enableIssued && feedback.driveState == 1) {
        preferDmBase = lastEnableViaBase;
        state = Running;
        txFailedAt = now;
        return;
    }
    if (Pending()) return;
    switch (startStage) {
    case SetDmMode:
        if (DmQuery(route.id, 0x55, 10, DriveMode(motion))) startStage = VerifyDmMode;
        break;
    case VerifyDmMode:
        if (pollModeNext) DmQuery(route.id, 0x33, 10);
        else DmQuery(route.id, 0xcc);
        pollModeNext = !pollModeNext;
        break;
    case PreloadPosition:
        if (!NativeTargetAllowed(motion, Position(motion), true)) {
            BeginStop(DriveRange, operateSeq);
        } else if (SendNative(Position(motion))) {
            startStage = EnableDrive;
            stageAt = now;
        }
        break;
    case QueryLkAngle:
        if (LkQuery(0x92, route.tx)) {
            lkAngleRequestAt = now;
            startStage = WaitLkAngle;
        }
        break;
    case WaitLkAngle:
        if (now - lkAngleRequestAt >= 35) startStage = QueryLkAngle;
        else LkQuery(0x9c, route.tx);
        break;
    case EnableDrive:
        if (!NativeTargetAllowed(motion, motion.target, true)) {
            BeginStop(DriveRange, operateSeq);
        } else if (route.family == Dm) {
            const bool useBase = (now - stageAt >= 45) != preferDmBase;
            if (Special(0xfc, useBase)) {
                enableIssued = true;
                lastEnableViaBase = useBase;
            }
        } else if (Special(0x88)) {
            enableIssued = true;
        }
        break;
    }
}

void ServiceRun() {
    if (!HostAlive() || !Fresh() || BusOff() || feedback.error || now - lastCycle > 15) {
        BeginStop(!HostAlive() ? HostLost : (BusOff() ? CanFailure : (feedback.error ? MotorFault : Timeout)), operateSeq);
        return;
    }
    if (HasTemperature() && feedback.temp >= motion.temperatureLimit) {
        BeginStop(OverTemperature, operateSeq);
        return;
    }
    const float position = Position(motion);
    if (motion.positionLimited && !InRange(position, motion.positionMin, motion.positionMax)) {
        BeginStop(PositionLimit, operateSeq);
        return;
    }
    if (now - lastSend < 5) return;
    lastSend = now;
    if (state == Starting) {
        ServiceStarting();
        if (state == Running) { Finish(operateSeq, Ok); operateSeq = 0; }
        return;
    }
    if (operateSeq) { Finish(operateSeq, Ok); operateSeq = 0; }
    const float sign = motion.reverse ? -1.0f : 1.0f;
    setpoint = motion.ramp > 0 ? Approach(setpoint, motion.target, motion.ramp * 0.005f) : motion.target;
    if (!motion.native) {
        speedReference = motion.mode == 1 ? setpoint : PID_Calc(&positionPid, setpoint, position);
        output = clamp(PID_Calc(&speedPid, speedReference, feedback.speed * sign / motion.ratio), -motion.limit, motion.limit) * sign;
    }
    if (!Pending() && (motion.native ? SendNative(setpoint) : SendOutput(output))) txFailedAt = now;
    else if (now - txFailedAt > 20) BeginStop(CanFailure, 0);
}

void Publish() {
    static uint32_t version;
    BenchStatus[0] = ++version;
    __DMB();
    BenchStatus[1] = now;
    BenchStatus[2] = state;
    BenchStatus[3] = ack;
    BenchStatus[4] = result;
    BenchStatus[5] = feedback.sequence;
    BenchStatus[6] = feedback.seen ? now - feedback.tick : NoFeedback;
    const float scale = (motion.reverse ? -1.0f : 1.0f) / motion.ratio;
    BenchStatus[7] = Bits(feedback.speed * scale);
    BenchStatus[8] = Bits((feedback.angle - feedback.zero) * scale);
    BenchStatus[9] = Bits(feedback.effort);
    BenchStatus[10] = Bits(feedback.temp);
    BenchStatus[11] = Bits(output);
    BenchStatus[12] = scanId;
    BenchStatus[13] = count;
    BenchStatus[14] = parameterCount;
    for (unsigned i = 0; i < 6; ++i) BenchStatus[15 + i] = transactionResult[i];
    BenchStatus[21] = (BusOff() ? 1u : 0u) | (DmCalibrated() ? 2u : 0u) |
                      (route.family == Dm && dmMode >= 1 && dmMode <= 4 ? 4u : 0u) |
                      (selected ? 8u : 0u) | (flashReady ? 16u : 0u) |
                      (selected && HasTemperature() ? 32u : 0u) | (lkZeroNeedsReload ? 64u : 0u) |
                      (Fresh() ? 128u : 0u);
    BenchStatus[22] = feedback.error;
    BenchStatus[23] = discoveryRevision;
    BenchStatus[24] = Bits(motion.target);
    BenchStatus[25] = route.port;
    BenchStatus[26] = route.family == Dm ? DriveTx(dmMode) : route.tx;
    BenchStatus[27] = Bits(setpoint);
    BenchStatus[28] = Bits(speedReference);
    __DMB();
    BenchStatus[0] = ++version;
}
}

void ApplicationEntryPoint() {
    if (BenchIdentity[0] != Magic) return;
    StartCan();
    flashReady = true;
    scanId = NoFeedback;
    lastCycle = HAL_GetTick() - 1;
    for (;;) {
        now = HAL_GetTick();
        if (now == lastCycle) continue;
        const uint32_t heartbeat = BenchHeartbeat;
        if (heartbeat != lastHeartbeat) {
            lastHeartbeat = heartbeat;
            heartbeatAt = now;
            hostHasPulse = true;
        }
        Frame frame{};
        for (unsigned n = 0; n < 64 && Receive(frame); ++n) {
            if (state == Scanning) ScanFrame(frame);
            if (selected) SelectedFrame(frame);
        }
        if (BenchCommand[0] != seenCommand) {
            uint32_t command[CommandWords];
            for (unsigned i = 0; i < CommandWords; ++i) command[i] = BenchCommand[i];
            seenCommand = command[0];
            Command(command);
        }
        switch (state) {
        case Scanning: ServiceScan(); break;
        case Starting: case Running: ServiceRun(); break;
        case Stopping: ServiceStop(); break;
        default:
            if (BusOff()) { Abort(); Recover(); }
            if (transaction.seq) ServiceParam();
            else if (selected) Monitor();
            break;
        }
        if ((now & 3u) == 0 || BenchStatus[3] != ack || BenchStatus[2] != state) Publish();
        lastCycle = now;
    }
}
}
