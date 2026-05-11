// ScrivCheck app launcher.
// Compiled binary is at Contents/MacOS/ScrivCheck.
// To rebuild: swiftc ScrivCheck.swift -o ScrivCheck
//
// Using a compiled binary (instead of a shell script) means macOS attributes
// Screen Recording permission to "ScrivCheck" rather than "python3".
import Foundation

var env = ProcessInfo.processInfo.environment
env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
let home = env["HOME"] ?? NSHomeDirectory()
let script = home + "/apps/scrivCheck/scrivcheck.py"
let logPath = NSTemporaryDirectory() + "scrivcheck_\(Int(Date().timeIntervalSince1970)).log"

let notify = Process()
notify.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
notify.arguments = ["-e", "display notification \"Validating backups…\" with title \"ScrivCheck\""]
try? notify.run(); notify.waitUntilExit()

FileManager.default.createFile(atPath: logPath, contents: nil)
let logHandle = try? FileHandle(forWritingTo: URL(fileURLWithPath: logPath))

let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
task.arguments = ["python3", script]
task.environment = env
task.standardOutput = logHandle
task.standardError = logHandle
try? task.run(); task.waitUntilExit()
try? logHandle?.close()

if task.terminationStatus != 0 {
    let content = (try? String(contentsOfFile: logPath, encoding: .utf8)) ?? ""
    let tail = content.components(separatedBy: "\n").filter { !$0.isEmpty }.suffix(5).joined(separator: "\n")
    let safe = tail.replacingOccurrences(of: "\"", with: "'")
    let alert = Process()
    alert.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    alert.arguments = ["-e", "display dialog \"ScrivCheck failed:\\n\(safe)\" buttons {\"OK\"} default button 1"]
    try? alert.run(); alert.waitUntilExit()
}
