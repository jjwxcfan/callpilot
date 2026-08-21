import Foundation

/// 运行时判定 APNs 环境。`#if DEBUG` 只是编译配置;token 真正归属哪个 APNs 环境由
/// 签名时写入 embedded.mobileprovision 的 aps-environment 决定。猜错的后果是 Worker
/// 打错 APNs 主机,收到 BadDeviceToken 后删除已存 token,推送从此静默失效(#118)。
enum ApnsEnvironmentDetector {
    static func detect(bundle: Bundle = .main) -> ApnsEnvironment {
        #if targetEnvironment(simulator)
        return .sandbox
        #else
        // App Store 签名会剥离 embedded.mobileprovision;TestFlight 保留 profile 但其
        // aps-environment 已是 production——两条分发路径都落在 production。
        guard let url = bundle.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url) else {
            return .production
        }
        if let environment = environment(fromProfileData: data) {
            return environment
        }
        #if DEBUG
        return .sandbox
        #else
        return .production
        #endif
        #endif
    }

    /// mobileprovision 是 CMS 包裹的 XML plist,aps-environment 键值以明文字节出现;
    /// isoLatin1 无损解码后正则提取,避免整套 CMS/ASN.1 解析。
    static func environment(fromProfileData data: Data) -> ApnsEnvironment? {
        guard let text = String(data: data, encoding: .isoLatin1) else { return nil }
        let pattern = /<key>aps-environment<\/key>\s*<string>(development|production)<\/string>/
        guard let match = text.firstMatch(of: pattern) else { return nil }
        return match.1 == "development" ? .sandbox : .production
    }
}
