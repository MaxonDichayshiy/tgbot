import SwiftUI
import UIKit

private struct AppIconOption: Identifiable {
    let id: String
    let title: String
    let subtitle: String
    let assetName: String
    let alternateName: String?

    static let all: [AppIconOption] = [
        .init(id: "classic", title: "Фирменный", subtitle: "Главный лаймовый знак", assetName: "SlopchikClassic", alternateName: nil),
        .init(id: "lime-invert", title: "Инверсия", subtitle: "Лайм на чёрном", assetName: "SlopchikLimeInvert", alternateName: "SlopchikLimeInvert"),
        .init(id: "purple", title: "Фиолетовый", subtitle: "Яркий характер", assetName: "SlopchikPurple", alternateName: "SlopchikPurple"),
        .init(id: "orange", title: "Оранжевый", subtitle: "Тёплый акцент", assetName: "SlopchikOrange", alternateName: "SlopchikOrange"),
        .init(id: "pink", title: "Розовый", subtitle: "Мягкий контраст", assetName: "SlopchikPink", alternateName: "SlopchikPink"),
        .init(id: "blue", title: "Синий", subtitle: "Холодный акцент", assetName: "SlopchikBlue", alternateName: "SlopchikBlue"),
        .init(id: "teal", title: "Бирюзовый", subtitle: "Спокойный оттенок", assetName: "SlopchikTeal", alternateName: "SlopchikTeal"),
        .init(id: "rainbow", title: "Радужный", subtitle: "Все цвета сразу", assetName: "SlopchikRainbow", alternateName: "SlopchikRainbow")
    ]
}

struct IconSettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var selectedIcon: String?
    @State private var isChanging = false
    @State private var errorMessage: String?
    @AppStorage("slopchik.theme") private var themeRaw = SlopThemeMode.system.rawValue

    private let columns = [GridItem(.flexible(), spacing: 14), GridItem(.flexible(), spacing: 14)]
    private let accent = SlopTheme.accent

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Выбери свой стиль")
                            .font(.custom("AvenirNext-Heavy", size: 25))
                        Text("Иконка изменится и на экране «Домой», и в библиотеке приложений.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    LazyVGrid(columns: columns, spacing: 14) {
                        ForEach(AppIconOption.all) { option in
                            iconCard(option)
                        }
                    }
                    if let errorMessage {
                        Text(errorMessage)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                    Text("Выбирай оттенок из фирменной палитры Слопчика.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(.top, 2)
                }
                .padding(18)
            }
            .background(SlopTheme.canvas.ignoresSafeArea())
            .navigationTitle("Иконка приложения")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { dismiss() }
                        .fontWeight(.semibold)
                        .tint(SlopTheme.accent)
                }
            }
            .onAppear {
                selectedIcon = UIApplication.shared.alternateIconName
            }
        }
    }

    private func iconCard(_ option: AppIconOption) -> some View {
        let isSelected = selectedIcon == option.alternateName
        return Button {
            changeIcon(to: option)
        } label: {
            VStack(alignment: .leading, spacing: 10) {
                ZStack(alignment: .topTrailing) {
                    Image(option.assetName)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                        .overlay {
                            RoundedRectangle(cornerRadius: 22, style: .continuous)
                                .stroke(isSelected ? SlopTheme.accent : SlopTheme.subtleBorder, lineWidth: isSelected ? 3 : 1)
                        }
                    if isSelected {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 25, weight: .semibold))
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(SlopTheme.accentForeground, SlopTheme.accent)
                            .padding(9)
                    }
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(option.title)
                        .font(.custom("AvenirNext-DemiBold", size: 16))
                        .foregroundStyle(.primary)
                    Text(option.subtitle)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(SlopTheme.surface, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).stroke(SlopTheme.subtleBorder, lineWidth: 0.8))
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(isChanging || isSelected)
        .accessibilityLabel("Выбрать иконку: \(option.title)")
    }

    private func changeIcon(to option: AppIconOption) {
        guard UIApplication.shared.supportsAlternateIcons else {
            errorMessage = "Эта версия iOS не поддерживает смену иконки."
            return
        }
        isChanging = true
        errorMessage = nil
        UIApplication.shared.setAlternateIconName(option.alternateName) { error in
            DispatchQueue.main.async {
                isChanging = false
                if let error {
                    errorMessage = "Не удалось сменить иконку: \(error.localizedDescription)"
                } else {
                    selectedIcon = option.alternateName
                }
            }
        }
    }
}
