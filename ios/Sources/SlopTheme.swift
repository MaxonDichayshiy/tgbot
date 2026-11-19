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
    // Brand book: lime #B6F23A, ink #111111, light canvas #F4F4F2.
    static let accent = Color(red: 182.0 / 255.0, green: 242.0 / 255.0, blue: 58.0 / 255.0)
    static let ink = Color(red: 17.0 / 255.0, green: 17.0 / 255.0, blue: 17.0 / 255.0)
    static let lightCanvas = Color(red: 244.0 / 255.0, green: 244.0 / 255.0, blue: 242.0 / 255.0)
    static let accentForeground = ink

    static let canvas = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 17.0 / 255.0, green: 17.0 / 255.0, blue: 17.0 / 255.0, alpha: 1)
            : UIColor(red: 244.0 / 255.0, green: 244.0 / 255.0, blue: 242.0 / 255.0, alpha: 1)
    })

    static let surface = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 28.0 / 255.0, green: 28.0 / 255.0, blue: 28.0 / 255.0, alpha: 1)
            : UIColor.white
    })

    static let subtleBorder = Color.primary.opacity(0.09)
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
