import './globals.css';

export const metadata = {
  title: 'Capture Review',
  description: 'What the ESP32-S3-BOX-3 actually recorded',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
