import SwiftUI

struct LoginView: View {
    let onLogin: (String, String, String) -> Void
    @Environment(\.colorScheme) private var colorScheme
    @State private var code = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    private let apiBase = "https://tgbot-naew.onrender.com"
    private let accent = SlopTheme.accent

    var body: some View {
        ZStack {
            SlopTheme.canvas.ignoresSafeArea()
            VStack(spacing: 24) {
                Spacer()
                ZStack {
                    RoundedRectangle(cornerRadius: 28).fill(.ultraThinMaterial)
                        .frame(width: 88, height: 88)
                        .shadow(color: .black.opacity(0.06), radius: 16, y: 6)
                    AnimatedMascotView(name: "SlopchikHappy").frame(width: 64, height: 64)
                }
                VStack(spacing: 8) {
                    Image(colorScheme == .dark ? "SlopchikLogoDark" : "SlopchikLogoLight")
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: 246)
                    Text("Твой личный помощник")
                        .font(.custom("AvenirNext-Medium", size: 15))
                        .foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text("Вход через Telegram").font(.system(size: 14, weight: .semibold)).foregroundStyle(.secondary)
                    Text("Открой Telegram, напиши боту команду /app и введи полученный код.")
                        .font(.system(size: 14)).foregroundStyle(.secondary)
                    SecureField("Одноразовый код", text: $code)
                        .textContentType(.oneTimeCode)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .padding(16).background(SlopTheme.inputSurface, in: RoundedRectangle(cornerRadius: 17, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 17, style: .continuous).stroke(SlopTheme.subtleBorder, lineWidth: 0.8))
                }
                if let errorMessage {
                    Text(errorMessage).font(.system(size: 13)).foregroundStyle(.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                Button(action: login) {
                    HStack {
                        if isLoading { ProgressView().tint(.white) }
                        Text(isLoading ? "Проверяем…" : "Продолжить").fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 17)
                    .foregroundStyle(SlopTheme.accentForeground)
                    .background(accent)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                }
                .disabled(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isLoading)
                .opacity(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.55 : 1)
                Spacer()
                Text("Слопчик • iOS").font(.footnote).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 28).padding(.vertical, 24)
        }
    }

    private func login() {
        guard let url = URL(string: "\(apiBase)/api/login") else { return }
        isLoading = true
        errorMessage = nil
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "code": code.trimmingCharacters(in: .whitespacesAndNewlines)
        ])
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isLoading = false
                if let error {
                    errorMessage = "Не удалось подключиться: \(error.localizedDescription)"
                    return
                }
                guard let data,
                      let http = response as? HTTPURLResponse,
                      (200...299).contains(http.statusCode),
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let receivedToken = result["token"] as? String else {
                    if let data,
                       let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let serverError = result["error"] as? String {
                        errorMessage = serverError
                    } else {
                        errorMessage = "Запросите новый код в Telegram командой /app."
                    }
                    return
                }
                let accountID = String(describing: result["user_id"] ?? "")
                guard !accountID.isEmpty else {
                    errorMessage = "Сервер не вернул идентификатор аккаунта. Обновите приложение."
                    return
                }
                onLogin(receivedToken, result["name"] as? String ?? "пользователь", accountID)
            }
        }.resume()
    }
}
