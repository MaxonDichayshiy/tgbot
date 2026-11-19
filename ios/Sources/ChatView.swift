import SwiftUI

private struct ChatMessage: Identifiable {
    let id = UUID()
    let role: String
    let text: String
}

struct ChatView: View {
    let token: String
    let userName: String
    let onLogout: () -> Void

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var isSending = false
    @State private var isLoading = true
    @State private var errorMessage: String?

    private let apiBase = "https://tgbot-naew.onrender.com"

    var body: some View {
        VStack(spacing: 0) {
            header
            if let errorMessage {
                Text(errorMessage).font(.footnote).foregroundStyle(.red)
                    .padding(.horizontal).padding(.vertical, 8)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 14) {
                        if isLoading {
                            ProgressView("Загружаем переписку…").padding(.top, 36)
                        } else if messages.isEmpty {
                            VStack(spacing: 10) {
                                Image(systemName: "bubble.left.and.bubble.right")
                                    .font(.system(size: 34)).foregroundStyle(.secondary)
                                Text("Начни новый разговор").font(.headline)
                                Text("Напиши Слопчику сообщение — он уже на связи.")
                                    .font(.subheadline).foregroundStyle(.secondary)
                                    .multilineTextAlignment(.center)
                            }
                            .padding(.top, 70).padding(.horizontal, 30)
                        }
                        ForEach(messages) { message in
                            messageBubble(message).id(message.id)
                        }
                    }
                    .padding(.horizontal, 14).padding(.vertical, 18)
                }
                .onChange(of: messages.count) { _ in
                    if let last = messages.last {
                        withAnimation(.easeOut(duration: 0.2)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }
            composer
        }
        .background(Color(red: 0.97, green: 0.97, blue: 0.98))
        .onAppear(perform: loadHistory)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "bubble.left.and.bubble.right.fill")
                .font(.system(size: 22))
                .foregroundStyle(Color(red: 0.30, green: 0.48, blue: 0.92))
            VStack(alignment: .leading, spacing: 2) {
                Text("Слопчик").font(.headline)
                Text("Вы вошли как \(userName)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button(action: resetChat) {
                Image(systemName: "plus.message").font(.system(size: 18, weight: .medium))
                    .frame(width: 38, height: 38).background(Color.white).clipShape(Circle())
            }
            .accessibilityLabel("Новый диалог")
            Menu {
                Button("Выйти", role: .destructive, action: onLogout)
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 18, weight: .semibold))
                    .frame(width: 38, height: 38).background(Color.white).clipShape(Circle())
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 12).background(.white)
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Сообщение…", text: $draft, axis: .vertical)
                .lineLimit(1...5).padding(.horizontal, 15).padding(.vertical, 12)
                .background(Color.white).clipShape(RoundedRectangle(cornerRadius: 22))
                .disabled(isSending)
            Button(action: sendMessage) {
                Group {
                    if isSending { ProgressView().tint(.white) }
                    else { Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold)) }
                }
                .frame(width: 46, height: 46).foregroundStyle(.white)
                .background(Color(red: 0.30, green: 0.48, blue: 0.92)).clipShape(Circle())
            }
            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isSending)
            .opacity(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.5 : 1)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }

    private func messageBubble(_ message: ChatMessage) -> some View {
        let isUser = message.role == "user"
        return HStack {
            if isUser { Spacer(minLength: 48) }
            Text(message.text).font(.system(size: 16))
                .foregroundStyle(isUser ? .white : .primary)
                .padding(.horizontal, 15).padding(.vertical, 11)
                .background(isUser ? Color(red: 0.30, green: 0.48, blue: 0.92) : .white)
                .clipShape(RoundedRectangle(cornerRadius: 18))
            if !isUser { Spacer(minLength: 48) }
        }
        .frame(maxWidth: .infinity)
    }

    private func authorizedRequest(_ url: URL, method: String = "GET") -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func loadHistory() {
        guard let url = URL(string: "\(apiBase)/api/history") else { return }
        var request = authorizedRequest(url)
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isLoading = false
                if let error {
                    errorMessage = "Не удалось загрузить историю: \(error.localizedDescription)"
                    return
                }
                guard let data,
                      let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let items = result["messages"] as? [[String: Any]] else {
                    errorMessage = "Не удалось получить историю. Попробуйте перезапустить приложение."
                    return
                }
                messages = items.compactMap { item in
                    guard let role = item["role"] as? String,
                          let text = item["text"] as? String, !text.isEmpty else { return nil }
                    return ChatMessage(role: role, text: text)
                }
            }
        }.resume()
    }

    private func sendMessage() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let url = URL(string: "\(apiBase)/api/chat") else { return }
        draft = ""
        isSending = true
        errorMessage = nil
        messages.append(ChatMessage(role: "user", text: text))
        var request = authorizedRequest(url, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 120
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                isSending = false
                if let error {
                    errorMessage = "Не удалось отправить сообщение: \(error.localizedDescription)"
                    return
                }
                guard let data,
                      let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    errorMessage = "Сервер не ответил. Попробуйте ещё раз."
                    return
                }
                if let serverError = result["error"] as? String {
                    errorMessage = serverError
                    return
                }
                let answer = result["text"] as? String ?? ""
                if !answer.isEmpty { messages.append(ChatMessage(role: "assistant", text: answer)) }
            }
        }.resume()
    }

    private func resetChat() {
        guard let url = URL(string: "\(apiBase)/api/reset") else { return }
        var request = authorizedRequest(url, method: "POST")
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                if let error {
                    errorMessage = "Не удалось начать новый диалог: \(error.localizedDescription)"
                    return
                }
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    errorMessage = "Не удалось очистить переписку."
                    return
                }
                messages = []
                errorMessage = nil
            }
        }.resume()
    }
}
