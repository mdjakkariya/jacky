/// main.swift — autobot-syscap entry point.
/// Parses CLI args, wires up SystemAudioTap, handles SIGTERM/SIGINT for clean shutdown.

import CoreAudio
import Foundation

// MARK: - Ignore SIGPIPE (broken pipe → write error rather than crash)
signal(SIGPIPE, SIG_IGN)

// MARK: - Argument parsing

func parseArgs() -> (sampleRate: Int, excludePID: Int) {
    var sampleRate = 16000
    var excludePID = 0
    let args = CommandLine.arguments.dropFirst()
    var it = args.makeIterator()
    while let arg = it.next() {
        switch arg {
        case "--sample-rate":
            if let val = it.next(), let n = Int(val), n > 0 { sampleRate = n }
        case "--exclude-pid":
            if let val = it.next(), let n = Int(val) { excludePID = n }
        default:
            break
        }
    }
    return (sampleRate, excludePID)
}

// MARK: - Runner (inside #available guard so the compiler accepts SystemAudioTap)

@available(macOS 14.2, *)
func run(sampleRate: Int, excludePID: Int) -> Never {
    let tap = SystemAudioTap(sampleRate: sampleRate, excludePID: excludePID)

    // Use DispatchSource for signal handling so we stay on the main thread
    // (avoids Swift exclusivity violations from C signal handlers).
    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)

    let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    let intSource  = DispatchSource.makeSignalSource(signal: SIGINT,  queue: .main)

    let shutdownHandler = {
        tap.stop()
        exit(0)
    }
    termSource.setEventHandler(handler: shutdownHandler)
    intSource.setEventHandler(handler: shutdownHandler)
    termSource.resume()
    intSource.resume()

    guard tap.start() else {
        fputs("error component=main msg=tap_start_failed\n", stderr)
        exit(1)
    }

    RunLoop.main.run()
    exit(0)  // unreachable
}

// MARK: - Entry point

if #available(macOS 14.2, *) {
    let (sr, pid) = parseArgs()
    run(sampleRate: sr, excludePID: pid)
} else {
    fputs("error component=main msg=requires_macos_14.2\n", stderr)
    exit(1)
}
