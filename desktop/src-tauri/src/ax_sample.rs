//! Bounded Accessibility-tree text sample for the focused app (macOS).
//!
//! Extracts titles and labels from the AX subtree under the focused window.
//! Requires Accessibility permission for Minion.

#[cfg(target_os = "macos")]
mod macos {
    use accessibility::{
        AXUIElement, AXUIElementAttributes, TreeWalker, TreeVisitor, TreeWalkerFlow,
    };
    use std::cell::{Cell, RefCell};

    pub fn focused_window_ax_text(pid: i32, max_chars: usize, max_depth: usize) -> Option<String> {
        if pid <= 0 {
            return None;
        }
        let app = AXUIElement::application(pid);
        let root = app.focused_window().or_else(|_| app.main_window()).ok()?;

        let collector = AxTextCollector {
            buf: RefCell::new(String::new()),
            max_chars,
            depth: Cell::new(0),
            max_depth,
        };
        TreeWalker::new().walk(&root, &collector);
        let s = collector.buf.borrow().trim().to_string();
        (!s.is_empty()).then_some(s)
    }

    struct AxTextCollector {
        buf: RefCell<String>,
        max_chars: usize,
        depth: Cell<usize>,
        max_depth: usize,
    }

    impl AxTextCollector {
        fn push_line(&self, piece: &str) {
            let t = piece.trim();
            if t.is_empty() {
                return;
            }
            let mut b = self.buf.borrow_mut();
            if b.len() >= self.max_chars {
                return;
            }
            if !b.is_empty() {
                b.push('\n');
            }
            let room = self.max_chars.saturating_sub(b.len());
            if room == 0 {
                return;
            }
            let safe: String = t.chars().take(room).collect();
            b.push_str(&safe);
        }
    }

    impl TreeVisitor for AxTextCollector {
        fn enter_element(&self, element: &AXUIElement) -> TreeWalkerFlow {
            if self.buf.borrow().len() >= self.max_chars {
                return TreeWalkerFlow::Exit;
            }
            let d = self.depth.get();
            if d >= self.max_depth {
                return TreeWalkerFlow::SkipSubtree;
            }
            self.depth.set(d + 1);

            if let Ok(t) = element.title() {
                self.push_line(&t.to_string());
            }
            if let Ok(t) = element.label_value() {
                self.push_line(&t.to_string());
            }
            if let Ok(t) = element.description() {
                self.push_line(&t.to_string());
            }
            if let Ok(t) = element.value_description() {
                self.push_line(&t.to_string());
            }

            TreeWalkerFlow::Continue
        }

        fn exit_element(&self, _element: &AXUIElement) {
            self.depth.set(self.depth.get().saturating_sub(1));
        }
    }
}

#[cfg(target_os = "macos")]
pub fn focused_window_ax_text(pid: i32, max_chars: usize, max_depth: usize) -> Option<String> {
    macos::focused_window_ax_text(pid, max_chars, max_depth)
}

#[cfg(not(target_os = "macos"))]
pub fn focused_window_ax_text(_pid: i32, _max_chars: usize, _max_depth: usize) -> Option<String> {
    None
}
