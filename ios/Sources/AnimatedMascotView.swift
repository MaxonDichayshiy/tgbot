import SwiftUI
import UIKit
import ImageIO

struct AnimatedMascotView: UIViewRepresentable {
    let name: String

    func makeUIView(context: Context) -> UIImageView {
        let view = UIImageView()
        view.contentMode = .scaleAspectFit
        view.clipsToBounds = true
        view.isAccessibilityElement = true
        view.accessibilityLabel = "Слопчик"
        return view
    }

    func updateUIView(_ imageView: UIImageView, context: Context) {
        guard imageView.accessibilityIdentifier != name else { return }
        imageView.stopAnimating()
        imageView.animationImages = nil
        imageView.accessibilityIdentifier = name

        guard let url = Bundle.main.url(forResource: name, withExtension: "png"),
              let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
            imageView.image = nil
            return
        }

        let frameCount = CGImageSourceGetCount(source)
        var frames: [UIImage] = []
        for index in 0..<frameCount {
            if let cgImage = CGImageSourceCreateImageAtIndex(source, index, nil) {
                frames.append(UIImage(cgImage: cgImage))
            }
        }

        guard let first = frames.first else { return }
        if frames.count > 1 {
            imageView.animationImages = frames
            imageView.animationDuration = max(Double(frames.count) * 0.12, 0.8)
            imageView.animationRepeatCount = 0
            imageView.startAnimating()
        } else {
            imageView.image = first
        }
    }
}
