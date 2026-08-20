// Functional-only models for the IHP SG13G2 cells present in the routed
// Hephaestus registered tiles. Timing, power, X/Z, and analog behavior are
// deliberately outside this formal model boundary.

module sg13g2_a21o_1(input A1, A2, B1, output X);
    assign X = (A1 & A2) | B1;
endmodule

module sg13g2_a21oi_1(input A1, A2, B1, output Y);
    assign Y = ~((A1 & A2) | B1);
endmodule

module sg13g2_a21oi_2(input A1, A2, B1, output Y);
    assign Y = ~((A1 & A2) | B1);
endmodule

module sg13g2_a221oi_1(input A1, A2, B1, B2, C1, output Y);
    assign Y = ~((A1 & A2) | (B1 & B2) | C1);
endmodule

module sg13g2_a22oi_1(input A1, A2, B1, B2, output Y);
    assign Y = ~((A1 & A2) | (B1 & B2));
endmodule

module sg13g2_and2_1(input A, B, output X);
    assign X = A & B;
endmodule

module sg13g2_and3_1(input A, B, C, output X);
    assign X = A & B & C;
endmodule

module sg13g2_and3_2(input A, B, C, output X);
    assign X = A & B & C;
endmodule

module sg13g2_and4_1(input A, B, C, D, output X);
    assign X = A & B & C & D;
endmodule

module sg13g2_buf_1(input A, output X);
    assign X = A;
endmodule

module sg13g2_buf_2(input A, output X);
    assign X = A;
endmodule

module sg13g2_buf_8(input A, output X);
    assign X = A;
endmodule

module sg13g2_buf_16(input A, output X);
    assign X = A;
endmodule

module sg13g2_dlygate4sd3_1(input A, output X);
    assign X = A;
endmodule

module sg13g2_inv_1(input A, output Y);
    assign Y = ~A;
endmodule

module sg13g2_mux2_1(input A0, A1, S, output X);
    assign X = S ? A1 : A0;
endmodule

module sg13g2_nand2_1(input A, B, output Y);
    assign Y = ~(A & B);
endmodule

module sg13g2_nand2_2(input A, B, output Y);
    assign Y = ~(A & B);
endmodule

module sg13g2_nand2b_1(input A_N, B, output Y);
    assign Y = ~(~A_N & B);
endmodule

module sg13g2_nand3_1(input A, B, C, output Y);
    assign Y = ~(A & B & C);
endmodule

module sg13g2_nand3b_1(input A_N, B, C, output Y);
    assign Y = ~(~A_N & B & C);
endmodule

module sg13g2_nand4_1(input A, B, C, D, output Y);
    assign Y = ~(A & B & C & D);
endmodule

module sg13g2_nor2_1(input A, B, output Y);
    assign Y = ~(A | B);
endmodule

module sg13g2_nor2_2(input A, B, output Y);
    assign Y = ~(A | B);
endmodule

module sg13g2_nor2b_1(input A, B_N, output Y);
    assign Y = ~(A | ~B_N);
endmodule

module sg13g2_nor2b_2(input A, B_N, output Y);
    assign Y = ~(A | ~B_N);
endmodule

module sg13g2_nor3_1(input A, B, C, output Y);
    assign Y = ~(A | B | C);
endmodule

module sg13g2_nor3_2(input A, B, C, output Y);
    assign Y = ~(A | B | C);
endmodule

module sg13g2_nor4_1(input A, B, C, D, output Y);
    assign Y = ~(A | B | C | D);
endmodule

module sg13g2_nor4_2(input A, B, C, D, output Y);
    assign Y = ~(A | B | C | D);
endmodule

module sg13g2_o21ai_1(input A1, A2, B1, output Y);
    assign Y = ~((A1 | A2) & B1);
endmodule

module sg13g2_or2_1(input A, B, output X);
    assign X = A | B;
endmodule

module sg13g2_or3_1(input A, B, C, output X);
    assign X = A | B | C;
endmodule

module sg13g2_or4_1(input A, B, C, D, output X);
    assign X = A | B | C | D;
endmodule

module sg13g2_tiehi(output L_HI);
    assign L_HI = 1'b1;
endmodule

module sg13g2_xnor2_1(input A, B, output Y);
    assign Y = ~(A ^ B);
endmodule

module sg13g2_xor2_1(input A, B, output X);
    assign X = A ^ B;
endmodule

// Official IHP description: positive-edge D flip-flop with low-active reset.
module sg13g2_dfrbpq_1(input CLK, D, RESET_B, output reg Q);
    always @(posedge CLK or negedge RESET_B) begin
        if (!RESET_B)
            Q <= 1'b0;
        else
            Q <= D;
    end
endmodule
