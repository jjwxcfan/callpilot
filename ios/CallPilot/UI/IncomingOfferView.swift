import SwiftUI

/// 来电接管请求全屏卡(对齐 Android IncomingOfferScreen)。
/// 前台展示与系统 CallKit 来电界面共享同一 offer 状态。
struct IncomingOfferView: View {
    @ObservedObject var model: AppModel
    let offer: InboundOffer

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "phone.arrow.up.right.fill")
                .font(.system(size: 56)).foregroundStyle(.green)
            Text(L10n.text("incoming.title")).font(.largeTitle).bold()
            if let context = model.incomingContext {
                TakeoverContextCard(context: context)
            } else {
                Text(L10n.text("incoming.description"))
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            HStack(spacing: 24) {
                Button {
                    model.dismissOffer(offer)
                } label: {
                    Label(L10n.text("incoming.decline"), systemImage: "phone.down.fill")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(.borderedProminent).tint(.red)

                Button {
                    Task { await model.answerTakeover(offer) }
                } label: {
                    Label(L10n.text("incoming.answer"), systemImage: "phone.fill")
                        .frame(maxWidth: .infinity, minHeight: 60)
                }
                .buttonStyle(.borderedProminent).tint(.green)
            }
            Text(L10n.text("incoming.decline_footer"))
                .font(.footnote).foregroundStyle(.secondary)
        }
        .padding(28)
    }
}

/// 来电者上下文卡片(WIL-137)。姓名一律带「自称」前缀——未核实的自报身份被显示成
/// 身份,等于替对方背书;有缺失项就整行不显示,不用「未知」占位骗自己有信息。
private struct TakeoverContextCard: View {
    let context: TakeoverContext

    var body: some View {
        VStack(spacing: 10) {
            if let name = TakeoverContextDisplay.callerName(context) {
                Text(name).font(.title3).bold().multilineTextAlignment(.center)
            }
            if context.claimedName != nil, let number = context.peerNumber {
                Text(number).font(.subheadline).foregroundStyle(.secondary)
            }
            if let purpose = context.purpose {
                Text(purpose)
                    .font(.body)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            Text(L10n.text("incoming.context.unverified"))
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 8)
        .accessibilityElement(children: .combine)
    }
}
