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
    static let accent = Color(red: 0.86, green: 0.36, blue: 0.16)
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
                        .stroke(Color.primary.opacity(0.07), lineWidth: 0.8)
                }
        }
    }
}

extension View {
    func slopGlass(cornerRadius: CGFloat = 22) -> some View {
        modifier(SlopGlassModifier(cornerRadius: cornerRadius))
    }
}
