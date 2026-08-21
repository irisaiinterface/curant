// curant-facetime-coreaudiotap.swift
//
// Second attempt at capturing FaceTime's call audio, using Core Audio
// PROCESS TAPS (AudioHardwareCreateProcessTap, macOS 14.2+) instead of
// ScreenCaptureKit.
//
// WHY A SECOND ATTEMPT, AND WHY THIS IS NOT THE SAME IDEA TWICE:
//
// Three capture routes have already been measured against live FaceTime
// calls on this Mac, each with a control proving the pipeline itself
// worked at that same moment:
//
//   1. Multi-Output Device -> BlackHole 16ch -> ffmpeg
//      FaceTime audio: RMS 0.0.  Control (tone into the same device,
//      same instant): RMS 5097.5.
//   2. ScreenCaptureKit scoped to FaceTime.app
//      489 buffers delivered at 48kHz/2ch -- the OS was actively
//      handing us audio -- peak amplitude 0 throughout.
//   3. ScreenCaptureKit capturing the whole system mix
//      During a call: peak 17 (~-65dB, dither).  Control, same binary,
//      music playing: peak 7614-8452.
//
// Conclusion from those: the capture code is correct, and macOS does
// not expose FaceTime call audio through either the default-output
// device path or ScreenCaptureKit.
//
// Core Audio process taps are a DIFFERENT mechanism, not a variation on
// those. ScreenCaptureKit is a screen/media-sharing API layered on top
// of the window server, and its audio path is governed by
// screen-recording policy. A process tap is a HAL-level construct: it
// asks Core Audio itself to duplicate a specific process's audio
// streams into an aggregate device. It is the API Apple added
// specifically so that audio tooling could capture other apps, and it
// is governed by the Audio Capture privacy class, not screen recording.
//
// That is a real reason to expect a different answer -- not a hope that
// running a similar thing again might behave differently. It may still
// be blocked: FaceTime's audio may be tagged in a way that excludes it
// from taps too. This binary is built to answer that question in one
// call rather than to assume either outcome, which is why it logs the
// same heartbeat-with-running-peak that made the ScreenCaptureKit
// result unambiguous.
//
// USAGE
//   curant-facetime-coreaudiotap --out-dir DIR [--segment-seconds 1.0]
//                                [--bundle-id com.apple.FaceTime]
//                                [--probe]
//
//   --probe  Run for 15 seconds, report whether any audio arrived and at
//            what amplitude, then exit. Use this for the yes/no test
//            before wiring it into anything.
//
// Writes DIR/turn_00000.wav, turn_00001.wav, ... at 16kHz mono int16 --
// the identical on-disk contract the ffmpeg segment muxer and the
// ScreenCaptureKit tap both produce, so curant-facetime-answerer.py can
// consume it with no changes beyond choosing the backend.
//
// Prints READY on stdout once the tap is running.

import Foundation
import AppKit          // NSWorkspace, for bundle id -> pid
import AVFoundation
import CoreAudio
import AudioToolbox

// MARK: - Small helpers

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("curant-facetime-coreaudiotap: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func log(_ message: String) {
    FileHandle.standardError.write(("curant-facetime-coreaudiotap: " + message + "\n").data(using: .utf8)!)
}

func osStatusName(_ status: OSStatus) -> String {
    // Core Audio returns four-character codes; printing the raw signed
    // integer alone has sent more than one person down the wrong path.
    let n = UInt32(bitPattern: status)
    let chars = [UInt8((n >> 24) & 0xff), UInt8((n >> 16) & 0xff), UInt8((n >> 8) & 0xff), UInt8(n & 0xff)]
    let printable = chars.allSatisfy { $0 >= 32 && $0 < 127 }
    let fourCC = printable ? String(bytes: chars, encoding: .ascii) ?? "" : ""
    return fourCC.isEmpty ? "\(status)" : "\(status) ('\(fourCC)')"
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr { fail("\(what) failed: \(osStatusName(status))") }
}

// MARK: - Arguments

var outDirPath: String?
var segmentSeconds: Double = 1.0
var bundleID = "com.apple.FaceTime"
var probeMode = false

var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let flag = args.removeFirst()
    switch flag {
    case "--out-dir":
        guard !args.isEmpty else { fail("--out-dir needs a value") }
        outDirPath = args.removeFirst()
    case "--segment-seconds":
        guard !args.isEmpty, let v = Double(args.removeFirst()) else { fail("--segment-seconds needs a number") }
        segmentSeconds = v
    case "--bundle-id":
        guard !args.isEmpty else { fail("--bundle-id needs a value") }
        bundleID = args.removeFirst()
    case "--probe":
        probeMode = true
    case "-h", "--help":
        print("""
        usage: curant-facetime-coreaudiotap --out-dir DIR [--segment-seconds 1.0]
                                            [--bundle-id com.apple.FaceTime] [--probe]

        Captures another application's audio using Core Audio process taps
        (macOS 14.2+) and writes rolling 16kHz mono WAV segments into DIR.

        --probe runs for 15s, reports whether audio arrived and its peak
        amplitude, then exits -- the quickest way to find out whether macOS
        will hand over this app's audio at all.
        """)
        exit(0)
    default:
        fail("unknown argument: \(flag)")
    }
}

if outDirPath == nil && !probeMode { fail("--out-dir is required (or use --probe)") }

guard #available(macOS 14.2, *) else {
    fail("Core Audio process taps require macOS 14.2 or newer. "
         + "Run `sw_vers -productVersion` to check.")
}

// MARK: - Locate the target process as a Core Audio object

func pidForBundleID(_ bundleID: String) -> pid_t? {
    for app in NSWorkspace.shared.runningApplications where app.bundleIdentifier == bundleID {
        return app.processIdentifier
    }
    return nil
}

/// Core Audio identifies processes by its own AudioObjectID, not by pid.
/// kAudioHardwarePropertyTranslatePIDToProcessObject does the mapping.
func audioObjectID(forPID pid: pid_t) -> AudioObjectID? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var inPID = pid
    var objectID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address,
        UInt32(MemoryLayout<pid_t>.size), &inPID, &size, &objectID)
    if status != noErr || objectID == kAudioObjectUnknown { return nil }
    return objectID
}

guard let targetPID = pidForBundleID(bundleID) else {
    fail("\(bundleID) is not running. Start a call first, then run this.")
}
guard let processObject = audioObjectID(forPID: targetPID) else {
    fail("Core Audio does not know about pid \(targetPID) (\(bundleID)). "
         + "That usually means the process has never produced audio.")
}
log("target: \(bundleID) pid \(targetPID) -> AudioObjectID \(processObject)")

// MARK: - Create the process tap

let tapDescription = CATapDescription(stereoMixdownOfProcesses: [processObject])
tapDescription.name = "Curant FaceTime Tap"
tapDescription.uuid = UUID()
// Private: the tap should not show up as a device other apps can see.
tapDescription.isPrivate = true
// Do NOT mute the process being tapped -- the human on this Mac should
// still hear the caller normally while Curant listens.
tapDescription.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
let tapStatus = AudioHardwareCreateProcessTap(tapDescription, &tapID)
if tapStatus != noErr {
    fail("AudioHardwareCreateProcessTap failed: \(osStatusName(tapStatus)). "
         + "If this is a permissions error, check System Settings > Privacy & Security "
         + "> Audio Recording (macOS may prompt on first run).")
}
log("created process tap, AudioObjectID \(tapID)")

func tapUID(_ tapID: AudioObjectID) -> CFString? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString>.size)
    var uid: CFString = "" as CFString
    let status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &uid)
    return status == noErr ? uid : nil
}

func tapStreamFormat(_ tapID: AudioObjectID) -> AudioStreamBasicDescription? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &asbd)
    return status == noErr ? asbd : nil
}

guard let uid = tapUID(tapID) else { fail("could not read the tap's UID") }
guard var sourceASBD = tapStreamFormat(tapID) else { fail("could not read the tap's stream format") }
log("tap format: \(Int(sourceASBD.mSampleRate))Hz, \(sourceASBD.mChannelsPerFrame)ch")

// MARK: - Wrap the tap in a private aggregate device

// A tap on its own produces no I/O; it has to be attached to an
// aggregate device, which is what actually gets an IOProc.
let aggregateUID = "curant.facetime.tap.\(UUID().uuidString)"
let aggregateDescription: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: "Curant FaceTime Capture",
    kAudioAggregateDeviceUIDKey as String: aggregateUID,
    kAudioAggregateDeviceIsPrivateKey as String: true,
    kAudioAggregateDeviceIsStackedKey as String: false,
    kAudioAggregateDeviceTapAutoStartKey as String: true,
    kAudioAggregateDeviceSubDeviceListKey as String: [],
    kAudioAggregateDeviceTapListKey as String: [
        [
            kAudioSubTapUIDKey as String: uid as String,
            kAudioSubTapDriftCompensationKey as String: true,
        ]
    ],
]

var aggregateID = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID),
      "AudioHardwareCreateAggregateDevice")
log("created private aggregate device \(aggregateID)")

// MARK: - Output plumbing (identical contract to the other backends)

let kTargetSampleRate: Double = 16000

guard let sourceFormat = AVAudioFormat(streamDescription: &sourceASBD) else {
    fail("could not build an AVAudioFormat from the tap's stream description")
}
guard let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: kTargetSampleRate,
                                       channels: 1,
                                       interleaved: true) else {
    fail("could not build the 16kHz mono target format")
}
guard let converter = AVAudioConverter(from: sourceFormat, to: targetFormat) else {
    fail("could not create a converter from \(sourceFormat) to \(targetFormat)")
}

final class SegmentWriter {
    private let outDir: URL
    private let framesPerSegment: AVAudioFrameCount
    private let settings: [String: Any]
    private var current: AVAudioFile?
    private var index = 0
    private var framesInCurrent: AVAudioFrameCount = 0

    init(outDir: URL, segmentSeconds: Double, format: AVAudioFormat) throws {
        self.outDir = outDir
        self.framesPerSegment = AVAudioFrameCount(segmentSeconds * format.sampleRate)
        self.settings = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: format.channelCount,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        try openNext()
    }

    private func openNext() throws {
        let target = outDir.appendingPathComponent(String(format: "turn_%05d.wav", index))
        try? FileManager.default.removeItem(at: target)
        current = try AVAudioFile(forWriting: target, settings: settings,
                                  commonFormat: .pcmFormatInt16, interleaved: true)
        framesInCurrent = 0
        index += 1
    }

    func write(_ buffer: AVAudioPCMBuffer) throws {
        guard buffer.frameLength > 0 else { return }
        try current?.write(from: buffer)
        framesInCurrent += buffer.frameLength
        if framesInCurrent >= framesPerSegment {
            // Releasing closes the file; the NEXT file appearing is the
            // signal curant-facetime-answerer.py waits on to know the
            // previous segment is complete.
            current = nil
            try openNext()
        }
    }
}

var writer: SegmentWriter?
if let outDirPath = outDirPath {
    let outDir = URL(fileURLWithPath: outDirPath, isDirectory: true)
    try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
    do {
        writer = try SegmentWriter(outDir: outDir, segmentSeconds: segmentSeconds, format: targetFormat)
    } catch {
        fail("could not open the first output segment: \(error.localizedDescription)")
    }
}

// MARK: - Capture

var sawAudio = false
var bufferCount = 0
var peakAllTime: Int = 0
var peakSinceHeartbeat: Int = 0
var lastHeartbeat = Date()
let stateLock = NSLock()

var ioProcID: AudioDeviceIOProcID?
let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, nil) {
    _, inInputData, _, _, _ in

    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
    guard let firstBuffer = abl.first, firstBuffer.mDataByteSize > 0 else { return }

    let bytesPerFrame = max(1, Int(sourceASBD.mBytesPerFrame))
    let frameCount = AVAudioFrameCount(Int(firstBuffer.mDataByteSize) / bytesPerFrame)
    guard frameCount > 0,
          let inBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: frameCount) else { return }
    inBuffer.frameLength = frameCount

    // Copy the incoming bytes into the AVAudioPCMBuffer.
    let dstABL = UnsafeMutableAudioBufferListPointer(inBuffer.mutableAudioBufferList)
    for (i, src) in abl.enumerated() where i < dstABL.count {
        if let s = src.mData, let d = dstABL[i].mData {
            memcpy(d, s, Int(min(src.mDataByteSize, dstABL[i].mDataByteSize)))
        }
    }

    let ratio = kTargetSampleRate / sourceFormat.sampleRate
    let capacity = AVAudioFrameCount((Double(frameCount) * ratio).rounded(.up)) + 64
    guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return }

    var supplied = false
    var convError: NSError?
    converter.convert(to: out, error: &convError) { _, status in
        if supplied { status.pointee = .noDataNow; return nil }
        supplied = true
        status.pointee = .haveData
        return inBuffer
    }
    if let convError = convError {
        log("conversion error: \(convError.localizedDescription)")
        return
    }

    stateLock.lock()
    if !sawAudio {
        sawAudio = true
        log("FIRST AUDIO BUFFER received (\(Int(sourceFormat.sampleRate))Hz, "
            + "\(sourceFormat.channelCount)ch) -- Core Audio IS delivering this process's audio")
    }
    bufferCount += 1
    if let ch = out.int16ChannelData {
        var peak = 0
        for i in 0..<Int(out.frameLength) { peak = max(peak, abs(Int(ch[0][i]))) }
        peakSinceHeartbeat = max(peakSinceHeartbeat, peak)
        peakAllTime = max(peakAllTime, peak)
    }
    if Date().timeIntervalSince(lastHeartbeat) >= 5.0 {
        lastHeartbeat = Date()
        log("heartbeat: \(bufferCount) buffers, peak since last heartbeat \(peakSinceHeartbeat), "
            + "peak all-time \(peakAllTime)")
        peakSinceHeartbeat = 0
    }
    stateLock.unlock()

    if let writer = writer {
        do { try writer.write(out) } catch { log("write error: \(error.localizedDescription)") }
    }
}
check(ioStatus, "AudioDeviceCreateIOProcIDWithBlock")
check(AudioDeviceStart(aggregateID, ioProcID), "AudioDeviceStart")

print("READY")
fflush(stdout)
log("capturing \(bundleID) -> " + (outDirPath ?? "(probe mode, not writing files)"))

func teardown() {
    if let ioProcID = ioProcID {
        AudioDeviceStop(aggregateID, ioProcID)
        AudioDeviceDestroyIOProcID(aggregateID, ioProcID)
    }
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
}

if probeMode {
    // Decisive yes/no in 15 seconds, with a verdict line rather than raw
    // numbers the reader has to interpret.
    DispatchQueue.global().asyncAfter(deadline: .now() + 15) {
        stateLock.lock()
        let saw = sawAudio, count = bufferCount, peak = peakAllTime
        stateLock.unlock()
        log("--- PROBE RESULT ---")
        if !saw {
            log("VERDICT: Core Audio delivered ZERO buffers for \(bundleID).")
            log("The tap was created successfully. Two possible causes, and they are")
            log("NOT the same thing -- rule out the boring one first:")
            log("  1. The app simply produced no audio during these 15s (paused,")
            log("     muted, or no call audio yet). Retry with audio ACTUALLY playing.")
            log("  2. macOS is declining to hand over this process's audio, which is")
            log("     the answer ScreenCaptureKit already gave for FaceTime.")
        } else if peak <= 2 {
            log("VERDICT: \(count) buffers arrived but peak amplitude was \(peak) (silence).")
            log("The stream exists and is empty -- FaceTime call audio is being excluded")
            log("from the tap, same signature as the ScreenCaptureKit result.")
        } else {
            log("VERDICT: SUCCESS -- \(count) buffers, peak amplitude \(peak).")
            log("Core Audio process taps DO expose \(bundleID)'s audio.")
            if bundleID == "com.apple.FaceTime" {
                log("This is the capture backend to use; wire it into")
                log("curant-facetime-answerer.py.")
            } else {
                log("That was a CONTROL run, not the real test: it proves the tap,")
                log("aggregate device, conversion and peak measurement all work.")
                log("Now run it against a live FaceTime call (no --bundle-id).")
            }
        }
        log("Control: run this against a music player to confirm the pipeline")
        log("itself works -- e.g. --bundle-id com.spotify.client --probe (Spotify)")
        log("or --bundle-id com.apple.Music --probe, with audio actually playing.")
        teardown()
        exit(saw && peak > 2 ? 0 : 3)
    }
}

for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { teardown(); exit(0) }
    src.resume()
    _ = Unmanaged.passRetained(src as AnyObject)
}

RunLoop.main.run()
