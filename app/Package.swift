// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "USBTether",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "USBTether", path: "Sources/USBTether")
    ]
)
