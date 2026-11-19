import SwiftUI
import UIKit

private struct AppIconOption: Identifiable {
    let id: String
    let title: String
    let subtitle: String
    let assetName: String
    let alternateName: String?

    static let all: [AppIconOption] = [
        .init(id: "classic", title: "Классика", subtitle: "Фирменный Слопчик", assetName: "SlopchikClassic", alternateName: nil),
        .init(id: "car", title: "Четырка", subtitle: "Слопчик и его легенда", assetName: "SlopchikCar", alternateName: "SlopchikCar"),
        .init(id: "yard", title: "Двор", subtitle: "Своя атмосфера", assetName: "SlopchikYard", alternateName: "SlopchikYard"),
        .init(id: "night", title: "Ночной", subtitle: "Тёмная версия", assetName: "SlopchikNight", alternateName: "SlopchikNight")
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
                            .font(.system(size: 25, weight: .bold, design: .rounded))
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
                    Text("Новые варианты будут появляться в следующих обновлениях Слопчика.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(.top, 2)
                }
                .padding(18)
            }
            .background(Color(uiColor: .systemGroupedBackground))
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
                                .stroke(isSelected ? Color(red: 0.86, green: 0.36, blue: 0.16) : Color.black.opacity(0.06), lineWidth: isSelected ? 3 : 1)
                        }
                    if isSelected {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 25, weight: .semibold))
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(.white, Color(red: 0.86, green: 0.36, blue: 0.16))
                            .padding(9)
                    }
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(option.title)
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(.primary)
                    Text(option.subtitle)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .slopGlass(cornerRadius: 20)
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
