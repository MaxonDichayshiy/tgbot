import SwiftUI
import UIKit

enum SlopThemeMode: String, CaseIterable {
    case system
    case light
    case dark

    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }
}

enum SlopTheme {
    // Brand book: lime #B6F23A, ink #111111, warm paper #F4F2EC.
    static let accent = Color(red: 182.0 / 255.0, green: 242.0 / 255.0, blue: 58.0 / 255.0)
    static let ink = Color(red: 17.0 / 255.0, green: 17.0 / 255.0, blue: 17.0 / 255.0)
    static let accentForeground = ink
    static let lightCanvas = Color(red: 244.0 / 255.0, green: 242.0 / 255.0, blue: 236.0 / 255.0)

    private static func adaptive(light: UIColor, dark: UIColor) -> Color {
        Color(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? dark : light
        })
    }

    static let canvas = adaptive(
        light: UIColor(red: 0.957, green: 0.949, blue: 0.925, alpha: 1),
        dark: UIColor(red: 0.067, green: 0.067, blue: 0.067, alpha: 1)
    )
    static let headerSurface = adaptive(
        light: UIColor(red: 0.957, green: 0.949, blue: 0.925, alpha: 1),
        dark: UIColor(red: 0.067, green: 0.067, blue: 0.067, alpha: 1)
    )
    static let composerSurface = adaptive(
        light: UIColor(red: 0.957, green: 0.949, blue: 0.925, alpha: 1),
        dark: UIColor(red: 0.067, green: 0.067, blue: 0.067, alpha: 1)
    )
    static let surface = adaptive(
        light: .white,
        dark: UIColor(red: 0.110, green: 0.114, blue: 0.102, alpha: 1)
    )
    static let assistantBubble = adaptive(
        light: .white,
        dark: UIColor(red: 0.114, green: 0.122, blue: 0.106, alpha: 1)
    )
    static let inputSurface = adaptive(
        light: .white,
        dark: UIColor(red: 0.122, green: 0.129, blue: 0.114, alpha: 1)
    )
    static let controlSurface = adaptive(
        light: UIColor(red: 0.922, green: 0.914, blue: 0.882, alpha: 1),
        dark: UIColor(red: 0.137, green: 0.145, blue: 0.125, alpha: 1)
    )
    static let subtleBorder = adaptive(
        light: UIColor.black.withAlphaComponent(0.075),
        dark: UIColor.white.withAlphaComponent(0.085)
    )
}

private struct SlopGlassModifier: ViewModifier {
    let cornerRadius: CGFloat

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content.glassEffect(.regular, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        } else {
            content
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                        .stroke(SlopTheme.subtleBorder, lineWidth: 0.8)
                }
        }
    }
}

extension View {
    func slopGlass(cornerRadius: CGFloat = 22) -> some View {
        modifier(SlopGlassModifier(cornerRadius: cornerRadius))
    }
}
