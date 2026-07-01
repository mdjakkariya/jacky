// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "autobot-syscap",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "autobot-syscap",
            path: "Sources/autobot-syscap",
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Info.plist"
                ])
            ]
        )
    ]
)
