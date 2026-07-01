/// SystemAudioTap.swift
/// Core Audio process tap + aggregate device + IOProc + resample-to-mono-int16 → stdout.

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

// MARK: - Helpers

private func osErr(_ label: String, _ status: OSStatus) {
    let code = Int32(bitPattern: UInt32(bitPattern: status))
    fputs("error component=\(label) osstatus=\(code)\n", stderr)
}

/// Read a CFString property from an AudioObject.
private func getStringProperty(
    objectID: AudioObjectID,
    selector: AudioObjectPropertySelector,
    scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: scope,
        mElement: kAudioObjectPropertyElementMain
    )
    var cfStr: CFString? = nil
    var size = UInt32(MemoryLayout<CFString?>.size)
    let status = withUnsafeMutablePointer(to: &cfStr) { ptr in
        AudioObjectGetPropertyData(objectID, &addr, 0, nil, &size, ptr)
    }
    guard status == noErr, let s = cfStr else { return nil }
    return s as String
}

/// Read an AudioStreamBasicDescription property.
private func getASBDProperty(
    objectID: AudioObjectID,
    selector: AudioObjectPropertySelector
) -> AudioStreamBasicDescription? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let status = AudioObjectGetPropertyData(objectID, &addr, 0, nil, &size, &asbd)
    guard status == noErr else { return nil }
    return asbd
}

/// Look up the AudioObjectID for a given PID by scanning kAudioHardwarePropertyProcessObjectList.
@available(macOS 14.2, *)
private func audioObjectIDForPID(_ pid: pid_t) -> AudioObjectID? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &dataSize
    )
    guard status == noErr, dataSize > 0 else { return nil }

    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
    var objectIDs = [AudioObjectID](repeating: 0, count: count)
    status = objectIDs.withUnsafeMutableBytes { ptr in
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &dataSize,
            ptr.baseAddress!
        )
    }
    guard status == noErr else { return nil }

    // For each process object, read its PID and compare
    for objID in objectIDs {
        var pidAddr = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyPID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var objPID: pid_t = 0
        var pidSize = UInt32(MemoryLayout<pid_t>.size)
        let pidStatus = AudioObjectGetPropertyData(objID, &pidAddr, 0, nil, &pidSize, &objPID)
        if pidStatus == noErr, objPID == pid {
            return objID
        }
    }
    return nil
}

// MARK: - SystemAudioTap

@available(macOS 14.2, *)
final class SystemAudioTap {
    private let targetSampleRate: Double
    private let excludePID: pid_t

    private var tapID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)
    private var aggID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID? = nil

    // AVAudioConverter (created once we know tap format)
    private var converter: AVAudioConverter? = nil
    private var dstFormat: AVAudioFormat? = nil
    private var tapChannelCount: UInt32 = 2

    // Stdout handle
    private let stdout = FileHandle.standardOutput

    init(sampleRate: Int, excludePID: Int) {
        self.targetSampleRate = Double(sampleRate)
        self.excludePID = pid_t(excludePID)
    }

    // MARK: - Start

    func start() -> Bool {
        // 1) Build excluded AudioObjectID list from PID
        var excludedIDs: [AudioObjectID] = []
        if excludePID != 0 {
            if let objID = audioObjectIDForPID(excludePID) {
                excludedIDs = [objID]
            } else {
                fputs("warning component=tap msg=exclude_pid_not_found pid=\(excludePID)\n", stderr)
            }
        }

        // 2) Create process tap
        let tapDesc: CATapDescription
        if excludedIDs.isEmpty {
            tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        } else {
            tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: excludedIDs)
        }
        tapDesc.muteBehavior = CATapMuteBehavior(rawValue: 0)!  // CATapUnmuted = 0

        var status = AudioHardwareCreateProcessTap(tapDesc, &tapID)
        guard status == noErr else {
            osErr("AudioHardwareCreateProcessTap", status)
            return false
        }

        // 3) Read tap UID
        guard let tapUID = getStringProperty(objectID: tapID, selector: kAudioTapPropertyUID) else {
            fputs("error component=tapUID msg=failed_to_read\n", stderr)
            return false
        }

        // 4) Read tap format (native float32 at device rate)
        guard var tapASBD = getASBDProperty(objectID: tapID, selector: kAudioTapPropertyFormat) else {
            fputs("error component=tapFormat msg=failed_to_read\n", stderr)
            return false
        }
        tapChannelCount = tapASBD.mChannelsPerFrame > 0 ? tapASBD.mChannelsPerFrame : 2

        // 5) Get default output device UID
        var defaultDevID = AudioDeviceID(kAudioObjectUnknown)
        var sysAddr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var devSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &sysAddr, 0, nil, &devSize, &defaultDevID
        )
        guard status == noErr, defaultDevID != kAudioObjectUnknown else {
            fputs("error component=defaultOutputDevice msg=not_found\n", stderr)
            return false
        }

        guard let outputUID = getStringProperty(
            objectID: defaultDevID, selector: kAudioDevicePropertyDeviceUID
        ) else {
            fputs("error component=outputUID msg=failed_to_read\n", stderr)
            return false
        }

        // 6) Create private aggregate device
        let aggUID = UUID().uuidString
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "AutobotSyscap",
            kAudioAggregateDeviceUIDKey: aggUID,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [
                [kAudioSubDeviceUIDKey: outputUID]
            ],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapDriftCompensationKey: true,
                    kAudioSubTapUIDKey: tapUID
                ]
            ]
        ]

        status = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
        guard status == noErr else {
            osErr("AudioHardwareCreateAggregateDevice", status)
            return false
        }

        // 7) Build AVAudioConverter (tap format → target rate mono int16)
        guard let srcFmt = AVAudioFormat(streamDescription: &tapASBD) else {
            fputs("error component=AVAudioFormat msg=srcFormat_invalid\n", stderr)
            return false
        }
        guard let dstFmt = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: targetSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            fputs("error component=AVAudioFormat msg=dstFormat_invalid\n", stderr)
            return false
        }
        guard let conv = AVAudioConverter(from: srcFmt, to: dstFmt) else {
            fputs("error component=AVAudioConverter msg=creation_failed\n", stderr)
            return false
        }
        self.dstFormat = dstFmt
        self.converter = conv

        let tapASBDCopy = tapASBD
        let srcFmtCopy = srcFmt

        // 8) Create IOProc
        status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { [weak self] (_, inInputData, _, _, _) in
            guard let self = self else { return }
            self.handleAudio(inInputData: inInputData, tapASBD: tapASBDCopy, srcFmt: srcFmtCopy)
        }
        guard status == noErr else {
            osErr("AudioDeviceCreateIOProcIDWithBlock", status)
            return false
        }

        // 9) Start device
        status = AudioDeviceStart(aggID, procID)
        guard status == noErr else {
            osErr("AudioDeviceStart", status)
            return false
        }

        // 10) Emit started event to stderr
        let startedJSON = "{\"event\":\"started\",\"sample_rate\":\(Int(targetSampleRate)),\"channels\":1}\n"
        fputs(startedJSON, stderr)

        return true
    }

    // MARK: - Audio callback

    private func handleAudio(
        inInputData: UnsafePointer<AudioBufferList>,
        tapASBD: AudioStreamBasicDescription,
        srcFmt: AVAudioFormat
    ) {
        guard let conv = converter, let dstFmt = dstFormat else { return }

        let abl = inInputData.pointee
        guard abl.mNumberBuffers > 0 else { return }

        let isNonInterleaved = (tapASBD.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        let numChannels = Int(tapASBD.mChannelsPerFrame > 0 ? tapASBD.mChannelsPerFrame : 2)

        // Compute frame count from first buffer
        let buf0 = withUnsafePointer(to: abl.mBuffers) { $0.pointee }
        let bytesPerFrame = isNonInterleaved
            ? (tapASBD.mBitsPerChannel / 8)
            : tapASBD.mBytesPerFrame
        let bpf = bytesPerFrame > 0 ? bytesPerFrame : 4
        let frameCount = AVAudioFrameCount(buf0.mDataByteSize / bpf)
        guard frameCount > 0 else { return }

        // Build source PCM buffer
        guard let srcBuf = AVAudioPCMBuffer(pcmFormat: srcFmt, frameCapacity: frameCount) else { return }
        srcBuf.frameLength = frameCount

        if isNonInterleaved {
            // Non-interleaved float32: copy each channel from its own AudioBuffer
            let bufListPtr = UnsafeBufferPointer<AudioBuffer>(
                start: withUnsafePointer(to: abl.mBuffers) { UnsafeRawPointer($0).assumingMemoryBound(to: AudioBuffer.self) },
                count: Int(abl.mNumberBuffers)
            )
            for (ch, abuf) in bufListPtr.enumerated() {
                guard ch < numChannels,
                      let srcData = abuf.mData,
                      let chData = srcBuf.floatChannelData?[ch] else { continue }
                memcpy(chData, srcData, Int(abuf.mDataByteSize))
            }
        } else {
            // Interleaved: single buffer, copy into first float channel slot
            if let srcData = buf0.mData,
               let chData = srcBuf.floatChannelData?[0] {
                memcpy(chData, srcData, Int(buf0.mDataByteSize))
            }
        }

        // Compute output capacity (with some headroom)
        let ratio = targetSampleRate / (tapASBD.mSampleRate > 0 ? tapASBD.mSampleRate : 44100.0)
        let outCapacity = AVAudioFrameCount(ceil(Double(frameCount) * ratio)) + 32
        guard let dstBuf = AVAudioPCMBuffer(pcmFormat: dstFmt, frameCapacity: outCapacity) else { return }

        var convError: NSError? = nil
        var srcConsumed = false

        let convStatus = conv.convert(to: dstBuf, error: &convError) { _, outStatus in
            if srcConsumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            outStatus.pointee = .haveData
            srcConsumed = true
            return srcBuf
        }

        guard convStatus != .error, dstBuf.frameLength > 0 else { return }

        // Write int16 LE samples to stdout
        guard let int16Data = dstBuf.int16ChannelData?[0] else { return }
        let byteCount = Int(dstBuf.frameLength) * MemoryLayout<Int16>.size
        let data = Data(bytes: int16Data, count: byteCount)

        do {
            try stdout.write(contentsOf: data)
        } catch {
            // Broken pipe or parent process died — exit cleanly
            exit(0)
        }
    }

    // MARK: - Stop

    func stop() {
        if let pid = procID {
            AudioDeviceStop(aggID, pid)
            AudioDeviceDestroyIOProcID(aggID, pid)
            procID = nil
        }
        if aggID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggID)
            aggID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }
}
