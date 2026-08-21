// curant-audiodiag.swift
//
// ONE diagnostic run, during ONE call, that tries every Core Audio
// capture target in sequence and reports which -- if any -- actually
// carries the caller's voice.
//
// WHY THIS EXISTS
//
// Every previous experiment cost a separate live test call, and each
// answered exactly one yes/no question. That is a slow way to search a
// space, and it has already burned several evenings. This walks the
// whole space in a single call.
//
// It also tests a hypothesis the earlier attempts skipped. The
// answerer's own module docstring records something important that was
// never followed up:
//
//     "FaceTime.app never runs for an incoming call -- confirmed via
//      ps aux: it's handled entirely by background daemons
//      (FTConversationService, facetimemessagestored, identityservicesd,
//      callservicesd) plus a system call-banner UI."
//
// If a DAEMON renders the call audio rather than FaceTime.app, then
// tapping com.apple.FaceTime was aimed at the wrong process from the
// start -- and "tap created successfully, zero buffers delivered" is
// exactly what that mistake would look like. Not a platform
// restriction at all. That distinction is the whole point of this tool.
//
// WHAT IT DOES, in order:
//
//   Phase 1  Enumerate every process Core Audio knows about, and report
//            which ones are ACTIVELY producing output right now. During
//            a live call, whichever process is rendering the caller's
//            voice must appear here. This alone may identify the target
//            without any guessing.
//   Phase 2  Global tap (every process at once, nothing excluded) for
//            10s. If this is silent while the call is audible, no
//            per-process tap can succeed either, and the answer is a
//            platform restriction.
//   Phase 3  Individually tap each process that looked active, ~6s
//            each, and report peak amplitude per process.
//   Phase 4  Print a ranked summary naming the process to target.
//
// USAGE
//   curant-audiodiag                 # full sweep, run DURING a call
//   curant-audiodiag --list-only     # just Phase 1, instant
//   curant-audiodiag --seconds 8     # per-process tap duration
//
// Speak continuously during the sweep, or have the caller speak --
// a phase measures nothing if nobody is talking through it.

import Foundation
import AppKit
import AVFoundation
import CoreAudio
import AudioToolbox

func out(_ s: String) {
    print(s)
    fflush(stdout)
}

func fourCC(_ status: OSStatus) -> String {
    let n = UInt32(bitPattern: status)
    let c = [UInt8((n >> 24) & 0xff), UInt8((n >> 16) & 0xff), UInt8((n >> 8) & 0xff), UInt8(n & 0xff)]
    let printable = c.allSatisfy { $0 >= 32 && $0 < 127 }
    let s = printable ? String(bytes: c, encoding: .ascii) ?? "" : ""
    return s.isEmpty ? "\(status)" : "\(status) ('\(s)')"
}

guard #available(macOS 14.2, *) else {
    out("Core Audio process taps require macOS 14.2+. This Mac is older.")
    exit(1)
}

var perProcessSeconds: Double = 6
var listOnly = false
var a = Array(CommandLine.arguments.dropFirst())
while !a.isEmpty {
    let f = a.removeFirst()
    switch f {
    case "--seconds": perProcessSeconds = Double(a.removeFirst()) ?? 6
    case "--list-only": listOnly = true
    case "-h", "--help":
        out("usage: curant-audiodiag [--seconds N] [--list-only]")
        out("Run DURING a live call, with someone speaking.")
        exit(0)
    default: out("unknown argument: \(f)"); exit(1)
    }
}

// MARK: - Phase 1: what does Core Audio think is making sound?

struct ProcInfo {
    let objectID: AudioObjectID
    let pid: pid_t
    let bundleID: String
    let name: String
    let runningOutput: Bool
    let runningInput: Bool
}

func processObjectList() -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &address, 0, nil, &size) == noErr else { return [] }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    var ids = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &address, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func uint32Prop(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = AudioObjectPropertyAddress(mSelector: selector,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var v: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(obj, &address, 0, nil, &size, &v) == noErr ? v : nil
}

func pidProp(_ obj: AudioObjectID) -> pid_t? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyPID,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var v: pid_t = 0
    var size = UInt32(MemoryLayout<pid_t>.size)
    return AudioObjectGetPropertyData(obj, &address, 0, nil, &size, &v) == noErr ? v : nil
}

func stringProp(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = AudioObjectPropertyAddress(mSelector: selector,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var cf: CFString? = nil
    let status = withUnsafeMutablePointer(to: &cf) { ptr -> OSStatus in
        AudioObjectGetPropertyData(obj, &address, 0, nil, &size, ptr)
    }
    guard status == noErr, let cf = cf else { return nil }
    return cf as String
}

func processName(pid: pid_t) -> String {
    if let app = NSRunningApplication(processIdentifier: pid) {
        if let n = app.localizedName { return n }
    }
    // Daemons aren't NSRunningApplications -- fall back to ps.
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/ps")
    p.arguments = ["-p", "\(pid)", "-o", "comm="]
    let pipe = Pipe()
    p.standardOutput = pipe
    try? p.run()
    p.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    let s = (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    return s.isEmpty ? "?" : (s as NSString).lastPathComponent
}

func gatherProcesses() -> [ProcInfo] {
    processObjectList().compactMap { obj in
        guard let pid = pidProp(obj) else { return nil }
        let bundle = stringProp(obj, kAudioProcessPropertyBundleID) ?? ""
        let runningOut = (uint32Prop(obj, kAudioProcessPropertyIsRunningOutput) ?? 0) != 0
        let runningIn = (uint32Prop(obj, kAudioProcessPropertyIsRunningInput) ?? 0) != 0
        return ProcInfo(objectID: obj, pid: pid, bundleID: bundle,
                        name: processName(pid: pid),
                        runningOutput: runningOut, runningInput: runningIn)
    }
}

out("=== PHASE 1: processes Core Audio knows about ===")
let procs = gatherProcesses()
if procs.isEmpty { out("  (none -- that itself is suspicious)") }
let active = procs.filter { $0.runningOutput || $0.runningInput }
for p in procs.sorted(by: { ($0.runningOutput ? 0 : 1, $0.name) < ($1.runningOutput ? 0 : 1, $1.name) }) {
    let flags = [p.runningOutput ? "OUTPUT" : nil, p.runningInput ? "input" : nil]
        .compactMap { $0 }.joined(separator: "+")
    let mark = p.runningOutput ? " <== producing audio now" : ""
    out(String(format: "  pid %-7d %-28@ %-34@ %@%@",
               p.pid, p.name as NSString, (p.bundleID.isEmpty ? "-" : p.bundleID) as NSString,
               flags.isEmpty ? "idle" : flags, mark))
}
out("")
out("Processes currently producing OUTPUT: \(active.filter { $0.runningOutput }.map { $0.name }.joined(separator: ", "))")
out("")
out("If a call is connected and audible right now, the process rendering the")
out("caller's voice MUST be one of the OUTPUT ones above. If FaceTime.app is")
out("not among them, that alone explains why tapping it returned nothing.")
out("")

if listOnly { exit(0) }

// MARK: - Tap helper

final class TapProbe {
    private(set) var buffers = 0
    private(set) var peak = 0
    private let lock = NSLock()
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var ioProc: AudioDeviceIOProcID?
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?
    private var asbd = AudioStreamBasicDescription()

    let label: String

    init(label: String) { self.label = label }

    func start(description: CATapDescription) -> String? {
        description.isPrivate = true
        description.muteBehavior = .unmuted
        description.name = "Curant Diag \(label)"
        description.uuid = UUID()

        var status = AudioHardwareCreateProcessTap(description, &tapID)
        if status != noErr { return "AudioHardwareCreateProcessTap: \(fourCC(status))" }

        var addr = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyUID,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<CFString?>.size)
        var cfUID: CFString? = nil
        status = withUnsafeMutablePointer(to: &cfUID) { p in
            AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, p)
        }
        guard status == noErr, let uid = cfUID else { return "read tap UID: \(fourCC(status))" }

        addr.mSelector = kAudioTapPropertyFormat
        size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        status = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd)
        guard status == noErr else { return "read tap format: \(fourCC(status))" }
        guard let src = AVAudioFormat(streamDescription: &asbd) else { return "bad tap format" }
        sourceFormat = src

        guard let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                         channels: 1, interleaved: true),
              let conv = AVAudioConverter(from: src, to: target) else { return "converter" }
        converter = conv

        let desc: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "Curant Diag \(label)",
            kAudioAggregateDeviceUIDKey as String: "curant.diag.\(UUID().uuidString)",
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceIsStackedKey as String: false,
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            kAudioAggregateDeviceSubDeviceListKey as String: [],
            kAudioAggregateDeviceTapListKey as String: [[
                kAudioSubTapUIDKey as String: uid as String,
                kAudioSubTapDriftCompensationKey as String: true,
            ]],
        ]
        status = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
        if status != noErr { return "AudioHardwareCreateAggregateDevice: \(fourCC(status))" }

        let bytesPerFrame = max(1, Int(asbd.mBytesPerFrame))
        status = AudioDeviceCreateIOProcIDWithBlock(&ioProc, aggID, nil) { [weak self] _, input, _, _, _ in
            guard let self = self, let src = self.sourceFormat, let conv = self.converter else { return }
            let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            guard let first = abl.first, first.mDataByteSize > 0 else { return }
            let frames = AVAudioFrameCount(Int(first.mDataByteSize) / bytesPerFrame)
            guard frames > 0, let inBuf = AVAudioPCMBuffer(pcmFormat: src, frameCapacity: frames) else { return }
            inBuf.frameLength = frames
            let dst = UnsafeMutableAudioBufferListPointer(inBuf.mutableAudioBufferList)
            for (i, s) in abl.enumerated() where i < dst.count {
                if let sp = s.mData, let dp = dst[i].mData {
                    memcpy(dp, sp, Int(min(s.mDataByteSize, dst[i].mDataByteSize)))
                }
            }
            guard let outBuf = AVAudioPCMBuffer(pcmFormat: conv.outputFormat,
                                                frameCapacity: frames + 64) else { return }
            var supplied = false
            var err: NSError?
            conv.convert(to: outBuf, error: &err) { _, st in
                if supplied { st.pointee = .noDataNow; return nil }
                supplied = true; st.pointee = .haveData; return inBuf
            }
            if err != nil { return }
            var p = 0
            if let ch = outBuf.int16ChannelData {
                for i in 0..<Int(outBuf.frameLength) { p = max(p, abs(Int(ch[0][i]))) }
            }
            self.lock.lock()
            self.buffers += 1
            self.peak = max(self.peak, p)
            self.lock.unlock()
        }
        if status != noErr { return "AudioDeviceCreateIOProcIDWithBlock: \(fourCC(status))" }
        status = AudioDeviceStart(aggID, ioProc)
        if status != noErr { return "AudioDeviceStart: \(fourCC(status))" }
        return nil
    }

    func stop() {
        if let ioProc = ioProc {
            AudioDeviceStop(aggID, ioProc)
            AudioDeviceDestroyIOProcID(aggID, ioProc)
        }
        if aggID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggID) }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
    }

    func snapshot() -> (Int, Int) {
        lock.lock(); defer { lock.unlock() }
        return (buffers, peak)
    }
}

func runProbe(_ label: String, seconds: Double, make: () -> CATapDescription) -> (Int, Int, String?) {
    let probe = TapProbe(label: label)
    if let err = probe.start(description: make()) {
        probe.stop()
        return (0, 0, err)
    }
    Thread.sleep(forTimeInterval: seconds)
    let (b, p) = probe.snapshot()
    probe.stop()
    return (b, p, nil)
}

// MARK: - Phase 2: global tap

out("=== PHASE 2: global tap (every process at once), 10s ===")
out("Speak now, or have the caller speak.")
let (gBuf, gPeak, gErr) = runProbe("global", seconds: 10) {
    CATapDescription(monoGlobalTapButExcludeProcesses: [])
}
if let gErr = gErr {
    out("  FAILED: \(gErr)")
} else {
    out("  buffers=\(gBuf)  peak=\(gPeak)")
    if gPeak > 50 {
        out("  => The global tap DOES carry real audio. Whatever is audible on this")
        out("     Mac is capturable this way, including (apparently) the call.")
    } else if gBuf > 0 {
        out("  => Stream exists but is silent. If the call was audible during these")
        out("     10s, macOS is excluding call audio from taps entirely.")
    } else {
        out("  => No buffers at all.")
    }
}
out("")

// MARK: - Phase 3: per-process taps

out("=== PHASE 3: individual taps, \(Int(perProcessSeconds))s each ===")
var results: [(String, Int, Int, String?)] = []
let candidates = procs.filter { $0.runningOutput }
    + procs.filter { !$0.runningOutput && (
        $0.bundleID.contains("FaceTime") || $0.name.lowercased().contains("facetime")
        || $0.name.lowercased().contains("callservices") || $0.name.lowercased().contains("avconference")
        || $0.name.lowercased().contains("identityservices")) }

if candidates.isEmpty {
    out("  No candidate processes. Is a call actually connected?")
}
for p in candidates {
    out("  probing \(p.name) (pid \(p.pid))\(p.bundleID.isEmpty ? "" : " [\(p.bundleID)]")...")
    let (b, pk, err) = runProbe("\(p.pid)", seconds: perProcessSeconds) {
        CATapDescription(stereoMixdownOfProcesses: [p.objectID])
    }
    results.append((("\(p.name) (pid \(p.pid))"), b, pk, err))
    if let err = err { out("     FAILED: \(err)") }
    else { out("     buffers=\(b)  peak=\(pk)") }
}
out("")

// MARK: - Phase 4: summary

out("=== SUMMARY ===")
out(String(format: "  %-40@ %8@ %8@", "target" as NSString, "buffers" as NSString, "peak" as NSString))
out(String(format: "  %-40@ %8d %8d", "GLOBAL (all processes)" as NSString, gBuf, gPeak))
for (name, b, p, err) in results.sorted(by: { $0.2 > $1.2 }) {
    if let err = err {
        out(String(format: "  %-40@ %@", name as NSString, "error: \(err)" as NSString))
    } else {
        out(String(format: "  %-40@ %8d %8d", name as NSString, b, p))
    }
}
out("")
let best = results.filter { $0.3 == nil }.max(by: { $0.2 < $1.2 })
if let best = best, best.2 > 50 {
    out("VERDICT: \(best.0) carries real audio (peak \(best.2)).")
    out("Tap THAT process, not com.apple.FaceTime, if it was audible call audio.")
} else if gPeak > 50 {
    out("VERDICT: only the GLOBAL tap carried audio. Use a global tap and accept")
    out("that other system sounds come with it, or exclude known noisy processes.")
} else {
    out("VERDICT: nothing carried real audio. If the call was genuinely audible")
    out("during this run, macOS is withholding call audio from Core Audio taps,")
    out("and this line of attack is finished -- move to real telephony.")
}
out("")
out("NOTE: a phase measures nothing if nobody spoke during it. If every number")
out("is near zero, re-run and talk continuously through the whole sweep.")
