class ContextStack:
    def __init__(self):
        self.last_context_id = 0
        # context id -> parent context id
        self.context_stack = {}
        # function name -> context id
        self.function_name_context = {}
        # function name -> declarer（用于兜底继承）
        self.function_declarer = {}

    def add_new_context(self, current):
        self.last_context_id += 1
        self.context_stack[self.last_context_id] = current
        return self.last_context_id

    def get_context(self, current, steps):
        context = current
        for _ in range(steps):
            context = self.context_stack.get(context, 0)
        return context

    def add_function_context(self, fn, current, declarer=None):
        """
        绑定函数到上下文：
        - 若未绑定，直接绑定；
        - 若已绑定为 0，而这次是非 0，则升级为非 0；
        - 若已绑定非 0，不会被 0 覆盖。
        """
        old = self.function_name_context.get(fn, None)
        if old is None:
            self.function_name_context[fn] = current
        else:
            if old == 0 and current != 0:
                self.function_name_context[fn] = current
        if declarer:
            self.function_declarer.setdefault(fn, declarer)

    def get_func_context(self, name, declarer=None):
        """
        优先返回已绑定上下文；否则沿声明者链向上找；否则绑定到根(0)。
        """
        if name in self.function_name_context:
            return self.function_name_context[name]
        seen = set()
        cur = declarer
        while cur and cur not in seen:
            seen.add(cur)
            if cur in self.function_name_context:
                ctx = self.function_name_context[cur]
                self.function_name_context[name] = ctx
                return ctx
            cur = self.function_declarer.get(cur, None)
        self.function_name_context[name] = 0
        return 0


function_context_stack = ContextStack()