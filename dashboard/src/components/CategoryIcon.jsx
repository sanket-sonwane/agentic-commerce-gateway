import {
  Milk, Carrot, Wheat, Croissant, Beef, CupSoda, Cookie, SprayCan, Egg, Fish, Package,
} from 'lucide-react'

const MAP = {
  dairy: { Icon: Milk },
  produce: { Icon: Carrot },
  staples: { Icon: Wheat },
  bakery: { Icon: Croissant },
  meat_eggs: { Icon: Beef },
  beverages: { Icon: CupSoda },
  snacks: { Icon: Cookie },
  household: { Icon: SprayCan },
}

export default function CategoryIcon({ category, size = 14 }) {
  const entry = MAP[category] || {}
  const Icon = entry.Icon || (category === 'meat_eggs' ? Egg : category === 'seafood' ? Fish : Package)
  return <Icon size={size} strokeWidth={2} />
}
