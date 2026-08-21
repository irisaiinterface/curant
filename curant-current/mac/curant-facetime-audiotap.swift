// curant-facetime-audiotap.swift
//
// Captures FaceTime's audio DIRECTLY from the application, using
// ScreenCaptureKit, and writes it as rolling 16kHz mono WAV segments --
// the same on-disk layout ffmpeg's segment muxer produced, so
// curant-facetime-answerer.py's existing turn loop works unchanged.
//
// WHY THIS EXISTS (the whole reason, stated plainly):
//
// The original design routed FaceTime's audio through a Multi-Output
// Device into BlackHole 16ch and captured that virtual device with
// ffmpeg. That approach was debugged across several nights and finally
// disproven on 2026-08-21 with a decisive measurement: during a live,
// connected FaceTime call, a test tone played into the system default
// output ("Curant Call Output") was captured back at RMS 5097.5 -- the
// capture path was provably perfect, mid-call -- while FaceTime's own
// call audio in that same window measured EXACTLY 0.0. Not faint. Not
// noisy. Zero samples.
//
// The only explanation consistent with that pair of numbers is that
// FaceTime does not render call audio into the system default output
// device at all. It is a VoIP client using the system's communications
// audio path, which bypasses aggregate/virtual output devices. No
// amount of setting the system default, rebuilding the Multi-Output
// Device, enabling drift correction, or changing WHEN the switch
// happens can change that -- and each of those was tried and failed.
//
// ScreenCaptureKit sidesteps the entire problem: it taps a specific
// application's audio at the OS level, wherever that app's audio is
// actually going. It does not care about default devices, aggregate
// devices, BlackHole, or routing. It also reuses the Screen Recording
// permission this feature ALREADY requires for its call detection
// (visual Accept-button matching), so it adds no new permission prompt
// for the customer.
//
// USAGE
//   curant-facetime-audiotap --out-dir DIR [--segment-seconds 1.0]
//                            [--bundle-id com.apple.FaceTime]
//
// Writes DIR/turn_00000.wav, turn_00001.wav, ... rotating every
// --segment-seconds. Runs until killed (SIGTERM/SIGINT), matching the
// lifetime of the persistent ffmpeg process it replaces.
//
// Prints "READY" to stdout once capture is actually running, so the
// Python side can distinguish "started and working" from "started and
// silently failed" -- a distinction that cost days of debugging with
// the previous backend.

import Foundation
import AVFoundation
import CoreMedia
import ScreenCaptureKit

// MARK: - Output format
//
// 16kHz mono int16 matches exactly what the Python pipeline already
// expects (it computes RMS on int16 frames and sends WAV to Gemini).
// Converting HERE rather than in Python means the segments on disk are
// already in final form -- no per-turn numpy channel extraction needed,
// which also removes the "which of 16 channels has the audio" guessing
// that the BlackHole path required.
let kTargetSampleRate: Double = 16000
let kTargetChannels: AVAudioChannelCount = 1

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("curant-facetime-audiotap: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func log(_ message: String) {
    FileHandle.standardError.write(("curant-facetime-audiotap: " + message + "\n").data(using: .utf8)!)
}

// MARK: - CMSampleBuffer -> AVAudioPCMBuffer

extension CMSampleBuffer {
    /// ScreenCaptureKit hands us CMSampleBuffers; AVAudioConverter wants
    /// AVAudioPCMBuffers. This bridges the two without copying the
    /// samples.
    var asPCMBuffer: AVAudioPCMBuffer? {
        try? withAudioBufferList { audioBufferList, _ -> AVAudioPCMBuffer? in
            guard let absd = formatDescription?.audioStreamBasicDescription else { return nil }
            guard let format = AVAudioFormat(standardFormatWithSampleRate: absd.mSampleRate,
                                             channels: absd.mChannelsPerFrame) else { return nil }
            return AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: audioBufferList.unsafePointer)
        }
    }
}

// MARK: - Rolling segment writer

/// Writes a continuous audio stream out as fixed-length WAV files named
/// turn_00000.wav, turn_00001.wav, ...
///
/// The naming and rotation behaviour are load-bearing, not cosmetic:
/// curant-facetime-answerer.py detects that segment N is complete by
/// watching for segment N+1 to APPEAR on disk (it never opens N to
/// check). So the next file must be created promptly when one closes,
/// which AVAudioFile does at init. Deviating from that pattern would
/// silently hang the Python turn loop.
final class SegmentWriter {
    private let outDir: URL
    private let framesPerSegment: AVAudioFrameCount
    private let settings: [String: Any]
    private let format: AVAudioFormat

    private var current: AVAudioFile?
    private var index = 0
    private var framesInCurrent: AVAudioFrameCount = 0

    init(outDir: URL, segmentSeconds: Double, format: AVAudioFormat) throws {
        self.outDir = outDir
        self.format = format
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

    private func url(for i: Int) -> URL {
        outDir.appendingPathComponent(String(format: "turn_%05d.wav", i))
    }

    private func openNext() throws {
        let target = url(for: index)
        try? FileManager.default.removeItem(at: target)
        current = try AVAudioFile(forWriting: target,
                                  settings: settings,
                                  commonFormat: .pcmFormatInt16,
                                  interleaved: true)
        framesInCurrent = 0
        index += 1
    }

    func write(_ buffer: AVAudioPCMBuffer) throws {
        guard buffer.frameLength > 0 else { return }
        try current?.write(from: buffer)
        framesInCurrent += buffer.frameLength
        if framesInCurrent >= framesPerSegment {
            // Close the finished file by releasing it, then immediately
            // create the next one -- that creation is the signal the
            // Python side is waiting on.
            current = nil
            try openNext()
        }
    }
}

// MARK: - Stream output

@available(macOS 13.0, *)
final class AudioTap: NSObject, SCStreamOutput, SCStreamDelegate {
    private let writer: SegmentWriter
    private let targetFormat: AVAudioFormat
    private var converter: AVAudioConverter?
    private var announcedReady = false
    private var sawAnyAudio = false

    init(writer: SegmentWriter, targetFormat: AVAudioFormat) {
        self.writer = writer
        self.targetFormat = targetFormat
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard sampleBuffer.isValid, let pcm = sampleBuffer.asPCMBuffer else { return }

        // Build the converter lazily: the source format is whatever
        // ScreenCaptureKit decides to hand us (typically 48kHz float32
        // stereo) and is only knowable once the first buffer arrives.
        if converter == nil || converter?.inputFormat != pcm.format {
            converter = AVAudioConverter(from: pcm.format, to: targetFormat)
            if converter == nil {
                log("could not create converter from \(pcm.format) to \(targetFormat)")
                return
            }
        }
        guard let converter = converter else { return }

        let ratio = targetFormat.sampleRate / pcm.format.sampleRate
        let capacity = AVAudioFrameCount((Double(pcm.frameLength) * ratio).rounded(.up)) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return }

        var supplied = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if supplied {
                status.pointee = .noDataNow
                return nil
            }
            supplied = true
            status.pointee = .haveData
            return pcm
        }
        if let error = error {
            log("conversion error: \(error.localizedDescription)")
            return
        }

        if !sawAnyAudio {
            sawAnyAudio = true
            log("first audio buffer received (\(Int(pcm.format.sampleRate))Hz, \(pcm.format.channelCount)ch)")
        }

        do {
            try writer.write(out)
        } catch {
            log("write error: \(error.localizedDescription)")
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("stream stopped with error: \(error.localizedDescription)")
        exit(2)
    }

    func markReady() {
        guard !announcedReady else { return }
        announcedReady = true
        print("READY")
        fflush(stdout)
    }
}

// MARK: - Argument parsing

var outDirPath: String?
var segmentSeconds: Double = 1.0
var bundleID = "com.apple.FaceTime"

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
    case "-h", "--help":
        print("""
        usage: curant-facetime-audiotap --out-dir DIR [--segment-seconds 1.0] [--bundle-id com.apple.FaceTime]

        Captures the named application's audio via ScreenCaptureKit and writes
        rolling 16kHz mono WAV segments (turn_00000.wav, turn_00001.wav, ...)
        into DIR. Prints READY on stdout once capture is live. Runs until killed.
        """)
        exit(0)
    default:
        fail("unknown argument: \(flag)")
    }
}

guard let outDirPath = outDirPath else { fail("--out-dir is required") }
guard segmentSeconds > 0.05 else { fail("--segment-seconds must be > 0.05") }

guard #available(macOS 13.0, *) else {
    fail("ScreenCaptureKit audio capture requires macOS 13 or newer.")
}

let outDir = URL(fileURLWithPath: outDirPath, isDirectory: true)
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

guard let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: kTargetSampleRate,
                                       channels: kTargetChannels,
                                       interleaved: true) else {
    fail("could not build the 16kHz mono target format")
}

// MARK: - Main

let sema = DispatchSemaphore(value: 0)
var streamRef: SCStream?

Task {
    do {
        // onScreenWindowsOnly: false matters -- a FaceTime AUDIO call
        // often has no ordinary on-screen window (its in-call UI is a
        // floating overlay), and filtering it out would leave nothing
        // to attach the audio filter to.
        let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                                          onScreenWindowsOnly: false)
        guard let app = content.applications.first(where: { $0.bundleIdentifier == bundleID }) else {
            fail("\(bundleID) is not running -- start a call first. "
                 + "(Found \(content.applications.count) running applications.)")
        }
        guard let display = content.displays.first else {
            fail("no display available to attach the capture filter to")
        }

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // We want audio only. SCStream still requires a video
        // configuration, so make it as close to free as possible: a
        // 2x2 frame at one frame per second costs essentially nothing
        // and is never read.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 6
        // Never capture Curant's own TTS -- otherwise every reply
        // Curant speaks would be recorded back as if the caller had
        // said it, and the barge-in detector would treat Curant's own
        // voice as the caller interrupting.
        config.excludesCurrentProcessAudio = true

        let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
        let writer = try SegmentWriter(outDir: outDir, segmentSeconds: segmentSeconds, format: targetFormat)
        let tap = AudioTap(writer: writer, targetFormat: targetFormat)

        let stream = SCStream(filter: filter, configuration: config, delegate: tap)
        try stream.addStreamOutput(tap, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "curant.audiotap"))
        try await stream.startCapture()
        streamRef = stream
        tap.markReady()
        log("capturing audio from \(app.applicationName) [\(bundleID)] "
            + "-> \(outDir.path) every \(segmentSeconds)s")
    } catch {
        fail("could not start capture: \(error.localizedDescription)")
    }
}

// Clean shutdown so the final segment is flushed rather than truncated.
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        if let s = streamRef {
            Task { try? await s.stopCapture(); sema.signal() }
        } else {
            sema.signal()
        }
    }
    src.resume()
    // Keep the source alive for the process lifetime.
    _ = Unmanaged.passRetained(src as AnyObject)
}

DispatchQueue.global().async {
    sema.wait()
    exit(0)
}

RunLoop.main.run()
