import SwiftUI

/// A colored dot showing the link state.
struct StatusDot: View {
    let state: TetherState

    private var color: Color {
        switch state {
        case .connected: return .green
        case .connecting, .reconnecting, .waiting: return .blue
        case .disabled, .notInstalled: return .gray
        case .serviceDown: return .orange
        case .failed: return .red
        }
    }

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 10, height: 10)
            .overlay(Circle().stroke(color.opacity(0.35), lineWidth: 4))
    }
}

/// A small line graph of recent speed samples. Both series share one scale so they compare fairly.
struct Sparkline: View {
    let values: [Double]
    let maxValue: Double
    let color: Color

    var body: some View {
        GeometryReader { geo in
            Path { path in
                guard values.count > 1 else { return }
                let top = max(maxValue, 1)
                let stepX = geo.size.width / CGFloat(values.count - 1)
                for (index, value) in values.enumerated() {
                    let x = CGFloat(index) * stepX
                    let y = geo.size.height * (1 - CGFloat(min(value, top) / top))
                    if index == 0 {
                        path.move(to: CGPoint(x: x, y: y))
                    } else {
                        path.addLine(to: CGPoint(x: x, y: y))
                    }
                }
            }
            .stroke(color, style: StrokeStyle(lineWidth: 1.6, lineJoin: .round))
        }
    }
}

/// Big speed number with an arrow, e.g. "↓ 1.2 MB/s".
struct SpeedBlock: View {
    let symbol: String
    let title: String
    let value: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: symbol).foregroundColor(color)
                Text(title).foregroundColor(.secondary)
            }
            .font(.caption)
            Text(value)
                .font(.system(size: 18, weight: .semibold, design: .rounded))
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// One "label ... value" row.
struct StatRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack {
            Text(label).foregroundColor(.secondary)
            Spacer()
            Text(value).monospacedDigit().textSelection(.enabled)
        }
        .font(.callout)
    }
}

/// A rounded notice bar, e.g. "service update available".
struct Banner<Content: View>: View {
    let tint: Color
    let content: Content

    init(tint: Color, @ViewBuilder content: () -> Content) {
        self.tint = tint
        self.content = content()
    }

    var body: some View {
        HStack(spacing: 8) { content }
            .font(.callout)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(tint.opacity(0.14)))
    }
}
