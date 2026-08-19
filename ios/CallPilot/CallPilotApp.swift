import SwiftUI

@main
struct CallPilotApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            RootView(callKit: appDelegate.callKit)
        }
    }
}

/// 根展示层:未配对时显示配对页;配对后保持主 Tab 壳常驻,通话与来电作为顶层覆盖。
struct RootView: View {
    @StateObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    @MainActor
    init(callKit: CallKitCoordinator) {
        _model = StateObject(wrappedValue: AppModel(callKit: callKit))
    }

    var body: some View {
        let presentation = RootPresentation.resolve(
            isPaired: model.pairing != nil,
            callState: model.callState,
            incomingOffer: model.incomingOffer
        )

        Group {
            if presentation == .pairing {
                PairView(model: model)
            } else {
                ZStack {
                    MainTabShell(model: model)
                        .allowsHitTesting(presentation == .main)
                        .accessibilityHidden(presentation != .main)

                    switch presentation {
                    case .call:
                        fullScreenOverlay { CallView(model: model) }
                    case .incomingOffer(let offer):
                        fullScreenOverlay { IncomingOfferView(model: model, offer: offer) }
                    case .pairing, .main:
                        EmptyView()
                    }
                }
            }
        }
        .task { await model.startOfferPolling() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { model.retryVoipTokenRegistrationIfNeeded() }
        }
    }

    private func fullScreenOverlay<Content: View>(
        @ViewBuilder content: () -> Content
    ) -> some View {
        ZStack {
            Color(uiColor: .systemBackground).ignoresSafeArea()
            content()
        }
    }
}
