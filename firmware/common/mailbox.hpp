#pragma once
#include <cstdint>

namespace motor_test {
constexpr uint32_t Protocol = 4;
constexpr uint32_t Magic = 0x4D425443;
constexpr unsigned CommandWords = 32;
constexpr unsigned StatusWords = 29;
constexpr unsigned DiscoverySize = 96;

enum Operation : uint32_t { Stop = 1, Scan, Select, Start, Target, Zero, ReadParam, WriteParam, SaveParams, NativeZero, Tune };
enum State : uint32_t { Idle, Scanning, Starting, Running, Stopping, Fault };
enum Result : uint32_t {
    Ok, Invalid, Offline, Busy, Timeout, ReadbackMismatch, WrongMode, CanFailure, HostLost, MotorFault, Conflict,
    OverTemperature, PositionLimit, MotorNotStill, ReloadZero, DriveRange
};
enum Family : uint32_t { Rm, Dm, Lk };

// All addresses are generated from the linked ELF. Command word 0 commits the payload.
struct Discovery {
    uint32_t id;
    uint32_t rx;
    uint32_t tx;
    uint32_t flags;
    uint32_t lastSeen;
};
static_assert(sizeof(Discovery) == 20);
}
