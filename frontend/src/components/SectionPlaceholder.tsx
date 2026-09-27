import { motion } from 'framer-motion';
import { BRAND } from '../ui/brand';

interface SectionPlaceholderProps {
  title: string;
  icon: string;
  status?: string;
  assignedBot?: string;
}

export const SectionPlaceholder = ({
  title,
  icon,
  status = 'ONLINE',
  assignedBot,
}: SectionPlaceholderProps) => (
  <div className="flex h-full w-full flex-col items-center justify-center gap-6 px-8 py-16">
    {/* Outer glow ring */}
    <motion.div
      className="relative flex items-center justify-center"
      initial={{ scale: 0.8, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ type: 'spring', stiffness: 350, damping: 30 }}
    >
      {/* Animated pulse ring */}
      <motion.div
        className="absolute rounded-full"
        style={{
          width: 120,
          height: 120,
          background: `radial-gradient(circle, ${BRAND.azure}33 0%, transparent 70%)`,
        }}
        animate={{ scale: [1, 1.25, 1], opacity: [0.6, 0.2, 0.6] }}
        transition={{ duration: 2.8, repeat: Infinity, ease: 'easeInOut' }}
      />
      {/* Icon container */}
      <div
        className="relative z-10 flex h-20 w-20 items-center justify-center rounded-2xl shadow-lg"
        style={{
          background: `linear-gradient(135deg, ${BRAND.dominant}22, ${BRAND.azure}33)`,
          border: `1.5px solid ${BRAND.azure}55`,
          boxShadow: `0 0 32px ${BRAND.azure}44`,
        }}
      >
        <span
          className="material-symbols-outlined text-4xl"
          style={{ color: BRAND.azure }}
        >
          {icon}
        </span>
      </div>
    </motion.div>

    {/* Title */}
    <motion.div
      className="flex flex-col items-center gap-2 text-center"
      initial={{ y: 16, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ type: 'spring', stiffness: 350, damping: 30, delay: 0.08 }}
    >
      <h1
        className="text-3xl font-bold tracking-tight"
        style={{ color: BRAND.dominant }}
      >
        {title}
      </h1>

      {assignedBot && (
        <p className="text-sm font-semibold uppercase tracking-widest opacity-50">
          Commanded by{' '}
          <span style={{ color: BRAND.azure }}>{assignedBot}</span>
        </p>
      )}
    </motion.div>

    {/* Status badge */}
    <motion.div
      className="flex items-center gap-2 rounded-full px-4 py-1.5 text-xs font-bold uppercase tracking-widest"
      style={{
        background: `${BRAND.azure}18`,
        border: `1px solid ${BRAND.azure}44`,
        color: BRAND.azure,
      }}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ delay: 0.18 }}
    >
      <motion.span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: BRAND.azure }}
        animate={{ opacity: [1, 0.3, 1] }}
        transition={{ duration: 1.6, repeat: Infinity }}
      />
      {status}
    </motion.div>

    {/* Construction notice */}
    <p className="max-w-xs text-center text-sm opacity-30">
      This module is being assembled. Full deployment incoming.
    </p>
  </div>
);
