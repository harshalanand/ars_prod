export default function Footer() {
  return (
    <div className="flex items-center justify-center gap-3 py-2 border-t border-gray-200 bg-gray-50/80 text-[8px] text-gray-400 select-none shrink-0">
      <span className="font-bold text-gray-500">ARS v2.0</span>
      <span className="text-gray-300">|</span>
      <span>Auto Replenishment System</span>
      <span className="text-gray-300">|</span>
      <span>Designed & Developed by <span className="font-semibold text-gray-500">Santosh Kumar</span></span>
      <span className="text-gray-300">|</span>
      <span>&copy; {new Date().getFullYear()} All rights reserved</span>
    </div>
  )
}
